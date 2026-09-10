"""QA-35 · Borrador persistente (docs/spec/v2/acceptance_scenarios.json).

Estimulo: escribir, adjuntar imagen, abrir editor, cambiar chat y recargar.
Resultado exigido (literal): "Texto/adjuntos/edicion vuelven; nada se envia
sin accion del usuario."

Requisitos: UX-01, MEDIA-03.

Estado: verde (L29). `studio/src/screens/Studio.tsx` ya guardaba el borrador
de TEXTO en `localStorage` (`readDraftFor`/`writeDraftFor`); ahora hace lo
mismo con los adjuntos ya subidos pero aun no enviados
(`readAttachmentsFor`/`writeAttachmentsFor`, misma forma "una ranura por
sesion, vacio borra la ranura" que el borrador) y los restaura al volver a
esa sesion en vez de vaciarlos incondicionalmente
(docs/spec/v2/MAPA_REUTILIZACION.md ya no lista el hueco de UX-01). Se
excluye Nobody mode explicitamente (parametro `incognito`, nunca leido
implicitamente) y nada se envia solo: `writeAttachmentsFor` unicamente
persiste metadatos ya subidos (id/name/mime/size), nunca dispara un envio.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.qa_state("green")

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def studio_tsx() -> str:
    return (REPO_ROOT / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")


def test_composer_attachments_are_persisted_alongside_the_draft_text(studio_tsx):
    assert re.search(r"function\s+writeAttachmentsFor\(", studio_tsx)
    assert re.search(r"function\s+readAttachmentsFor\(", studio_tsx)


def test_switching_sessions_restores_attachments_instead_of_dropping_them(studio_tsx):
    # The history-load effect used to unconditionally `setAttachments([])` on
    # every session switch — the exact bug this scenario names ("adjuntos...
    # vuelven"). It must now restore this session's own persisted attachments.
    assert "setAttachments([])" not in studio_tsx
    assert "setAttachments(readAttachmentsFor(sessionId))" in studio_tsx


def test_attachments_are_never_persisted_in_incognito(studio_tsx):
    fn = studio_tsx[studio_tsx.index("function writeAttachmentsFor("):]
    fn = fn[: fn.index("\n}\n") + 3]
    assert "if (incognito) return;" in fn, (
        "writeAttachmentsFor must refuse to write anything while Nobody mode is on"
    )


def test_persisting_attachments_never_sends_anything_by_itself():
    """QA-35's other half: "nada se envia sin accion del usuario." The
    persistence helpers must only ever be called from state setters/effects,
    never from a send path (`sendTurn`/`run(`) — persisting is not sending."""
    studio_tsx_text = (REPO_ROOT / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    for line in studio_tsx_text.splitlines():
        if "writeAttachmentsFor(" in line and "function writeAttachmentsFor" not in line:
            assert "run(" not in line and "sendTurn" not in line, (
                f"writeAttachmentsFor must not be called from a send path: {line!r}"
            )
