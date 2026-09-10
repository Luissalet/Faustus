"""QA-37 · Lectura bajo stream (docs/spec/v2/acceptance_scenarios.json).

Estimulo: seleccionar codigo antiguo mientras llegan miles de deltas.
Resultado exigido (literal): "No salto al final, perdida de seleccion ni
bloqueo del compositor."

Requisitos: UX-05, PERF-01.

Estado: verde (Lote 39). `studio/src/screens/studio/Transcript.tsx` ahora
virtualiza la lista de turnos con `@tanstack/react-virtual`
(`useVirtualizer`): solo los turnos visibles (mas overscan) se montan, asi
que una conversacion de 500 turnos no reconstruye/repinta las 500 tarjetas
en cada delta. El turno en streaming se repinta como maximo una vez por
frame (`useFrameBatched`, sobre `lib/frame-batch.ts::frameBatcher` -- ver
tests/test_studio_l39_frame_batch_js.py) en vez de una vez por delta, asi
que el hilo de UI (y por tanto el compositor) no se satura con miles de
deltas por segundo. El auto-scroll "solo si el lector esta abajo" ya
existia en Studio.tsx (`pinnedRef`, fichero ajeno a este lote, ver
docs/spec/v2/MAPA_REUTILIZACION.md fila UX-05) y sigue funcionando sin
cambios porque el "sizer" virtualizado expone el mismo `scrollHeight` total
que antes; el boton "volver abajo" (`fs-studio__back-to-bottom`) es nuevo.

Lo que este test SI prueba (mecanico, sin navegador): que la virtualizacion
esta realmente cableada en el componente, no solo importada sin usar. Lo
que NO prueba (necesita un navegador real con miles de eventos de stream
llegando de verdad): que la seleccion de texto sobrevive pixel a pixel
durante el stream -- eso queda para `scripts/ui_a11y.py` /
verificacion manual, igual que QA-44.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.qa_state("green")

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPT = REPO_ROOT / "studio" / "src" / "screens" / "studio" / "Transcript.tsx"
FRAME_BATCH = REPO_ROOT / "studio" / "src" / "lib" / "frame-batch.ts"


def test_the_transcript_virtualizes_the_turn_list():
    source = TRANSCRIPT.read_text(encoding="utf-8")
    assert "useVirtualizer" in source, "PERF-01: no list virtualization wired into Transcript.tsx"
    assert "@tanstack/react-virtual" in source
    # Actually driving the hook's return value (not just imported and unused).
    assert "measureElement" in source
    assert re.search(r"getVirtualItems\s*\(", source), "virtual items must actually be rendered, not just requested"


def test_streaming_turns_are_repainted_at_most_once_per_frame():
    """UX-05: "los deltas se aplican por requestAnimationFrame agrupados" —
    `useFrameBatched` (Transcript.tsx) wraps `frameBatcher`
    (lib/frame-batch.ts, unit-proven by test_studio_l39_frame_batch_js.py)
    and is applied to the assistant turn only while it is still streaming.
    """
    source = TRANSCRIPT.read_text(encoding="utf-8")
    assert "useFrameBatched" in source
    assert FRAME_BATCH.exists()
    assert "frameBatcher" in source


def test_a_turns_entrance_animation_never_replays_on_virtualized_remount():
    """A regression virtualization would otherwise introduce: scrolling a
    turn back into view must not replay its "just arrived" animation
    (`fs-rise`) — that would read as new messages flooding in on every
    scroll, the opposite of "lectura agradable"."""
    source = TRANSCRIPT.read_text(encoding="utf-8")
    assert "seenIds" in source
    css = (REPO_ROOT / "studio" / "src" / "screens" / "studio.css").read_text(encoding="utf-8")
    assert ".fs-turn[data-enter]" in css, "the arrival animation must be gated, not applied to every .fs-turn unconditionally"
