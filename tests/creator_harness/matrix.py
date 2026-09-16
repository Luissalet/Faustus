"""tests/creator_harness/matrix.py — WP36: the minimum failure matrix.

Transcribed 1:1 from `docs/spec/creator/plan/docs/08_PRUEBAS_Y_ACEPTACION.md`,
"Matriz mínima de fallos" — the eleven rows below are the SAME frontier /
fault injection / expected result the doc lists (frontiers this harness
does not own, like Autorización/Contexto/Import, are noted `owner="other_wp"`
and skipped rather than faked here — a test asserting a WP36-owned outcome
for a WP09/WP04/WP34 boundary would be testing the wrong module).

`registered_adapters()` walks `src.creator.adapters.registry()` (WP10's own
pkgutil discovery — this module adds no second list to keep in sync) so a
new adapter dropped into `src/creator/adapters/` is automatically covered by
`test_failure_matrix.py`/`test_adapter_contract.py` the next time the suite
runs, with no edit required here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

__all__ = ["MatrixRow", "MATRIX", "registered_adapters", "rows_for_adapter"]


@dataclass(frozen=True)
class MatrixRow:
    """One row of the minimum failure matrix."""

    id: str
    frontier: str          # "Plan" | "Submit" | "Cancelación" | "Collect" | "Recursos" | ...
    fault: str              # what is injected
    expected: str            # what a correct adapter/caller must do about it
    owner: str = "wp36"      # "wp36" (this package owns proving it) | "other_wp" (documented, not faked here)
    fake_scenario: str = ""  # matching `fake_engines.SCENARIOS` entry, if any


MATRIX: Tuple[MatrixRow, ...] = (
    MatrixRow(
        id="plan_missing_capability",
        frontier="Plan",
        fault="Capability ausente o constraint incompatible.",
        expected="Refusal explícito (`AdapterPlan.ok=False`) sin modelo cargado ni llamada generativa.",
        owner="wp36",
    ),
    MatrixRow(
        id="auth_input_changed_after_approval",
        frontier="Autorización",
        fault="Cambiar input, destinatario, modelo o revisión tras aprobar.",
        expected="Approval invalidado para esa operación.",
        owner="other_wp",  # src/approval_store.py / src/tool_approvals.py (WP09) own this frontier
    ),
    MatrixRow(
        id="submit_response_lost",
        frontier="Submit",
        fault="Engine acepta pero la respuesta se pierde.",
        expected="Estado incierto (`accepted_uncertain`/`submit_unknown`); reconciliar, no duplicar el render.",
        owner="wp36",
        fake_scenario="submit_lost_response",
    ),
    MatrixRow(
        id="execution_worker_restart_loses_lease",
        frontier="Ejecución",
        fault="Worker reinicia o pierde su lease.",
        expected="Resultado antiguo no puede aplicarse a otro intento (job id fencing).",
        owner="wp36",
        fake_scenario="engine_crash_mid_job",
    ),
    MatrixRow(
        id="cancel_races_completion",
        frontier="Cancelación",
        fault="Completar al mismo tiempo que cancelar.",
        expected="Transición canónica consistente y efecto externo descrito honestamente.",
        owner="wp36",
        fake_scenario="cancel_races_completion",
    ),
    MatrixRow(
        id="cancel_arrives_too_late",
        frontier="Cancelación",
        fault="Cancel llega después de que el job ya completó.",
        expected="`too_late`, nunca `confirmed`/`accepted` de un stop que no ocurrió.",
        owner="wp36",
        fake_scenario="cancel_too_late",
    ),
    MatrixRow(
        id="collect_disk_full",
        frontier="Collect",
        fault="Disco lleno o descarga interrumpida.",
        expected="Temporal verificable; no anunciar completed de un archivo inexistente.",
        owner="wp36",
        fake_scenario="disk_full",
    ),
    MatrixRow(
        id="collect_corrupt_output",
        frontier="Collect",
        fault="El motor produce una salida corrupta.",
        expected="`collect()` marca `valid=False`/`ok=False`; nunca se registra como artefacto.",
        owner="wp36",
        fake_scenario="corrupt_output",
    ),
    MatrixRow(
        id="collect_truncated_output",
        frontier="Collect",
        fault="La descarga se trunca a mitad.",
        expected="`collect()` marca `valid=False`/`ok=False`; nunca se registra como artefacto.",
        owner="wp36",
        fake_scenario="truncated_output",
    ),
    MatrixRow(
        id="artifacts_two_owners_same_hash",
        frontier="Artefactos",
        fault="Dos owners comparten bytes iguales.",
        expected="Acceso separado a occurrences; no filtración por hash.",
        owner="other_wp",  # src/artifact_identity.py / WP03 own occurrence separation
    ),
    MatrixRow(
        id="edit_concurrent_revision",
        frontier="Edición",
        fault="UI y agente escriben la misma revisión.",
        expected="409/ramas; no último escritor gana.",
        owner="other_wp",  # src/creator/store.py (WP02) apply_command owns this
    ),
    MatrixRow(
        id="context_stale_handoff",
        frontier="Contexto",
        fault="Handoff apunta a una versión antigua.",
        expected="Delta de cambios y revalidación antes de continuar.",
        owner="other_wp",  # WP27 goal/handoff owns this
    ),
    MatrixRow(
        id="resources_two_endpoints_same_gpu",
        frontier="Recursos",
        fault="Dos endpoints usan la misma GPU.",
        expected="Una sola capacidad física y cola/rechazo conservador.",
        owner="wp36",  # covered by test_resources_under_failure.py against src/creator/resources.py (WP30)
    ),
    MatrixRow(
        id="local_only_plugin_uploads",
        frontier="Local-only",
        fault="Plugin o custom node intenta subir un asset.",
        expected="Bloqueo real de salida o adapter no autorizado, no simple aviso.",
        owner="other_wp",  # WP32 plugin lifecycle owns this
    ),
    MatrixRow(
        id="import_zip_path_traversal",
        frontier="Import",
        fault="ZIP path traversal, SVG activo, streams enormes.",
        expected="Rechazo acotado y ningún efecto fuera del root.",
        owner="other_wp",  # WP04 ingest owns this
    ),
)


def registered_adapters() -> Dict[str, Callable]:
    """`{module_name: factory}` from `src.creator.adapters.registry()` — the
    SAME discovery WP10's routes/production_runs use, so this harness never
    drifts from what is actually wired up."""
    from src.creator import adapters as adapters_pkg
    return adapters_pkg.registry()


def rows_for_adapter(owner: str = "wp36") -> List[MatrixRow]:
    """The rows this package is responsible for proving (`owner="wp36"` by
    default) — the `"other_wp"` rows exist here for traceability against
    the doc, not to be faked by an adapter this package does not own."""
    return [row for row in MATRIX if row.owner == owner]
