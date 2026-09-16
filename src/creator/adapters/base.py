"""adapters/base.py — WP10: shared adapter infrastructure.

Two things every adapter needs and none of them should reimplement:

* `BaseAdapter` — a default `reconcile()` that falls back to `status()`
  (the honest answer for an adapter whose manifest says
  `supports_reconcile=False`: "reconciling" is just asking again).
* `stage_inputs()` — the ONE place a resolved occurrence id becomes a
  filesystem path. `docs/03_ARQUITECTURA_Y_CONTRATOS.md`: "`submit` recibe
  IDs de artefactos resueltos y staging controlado, nunca rutas arbitrarias
  del modelo." An adapter's `submit()` reads `staging.input_paths[occ_id]`;
  it never calls `artifact_identity` itself and never accepts a path from a
  request body.
"""
from __future__ import annotations

import os
from typing import Sequence

from src.creator.adapter_port import StatusResult, Staging, new_staging_dir


class BaseAdapter:
    """Not required by the `AdapterPort` protocol (a `Protocol` is
    structural), but every real adapter in this package subclasses it so a
    manifest that says `supports_reconcile=False` still has a working,
    honest `reconcile()` rather than a `NotImplementedError`."""

    name: str = "base"

    def reconcile(self, job_id: str) -> StatusResult:
        return self.status(job_id)  # type: ignore[attr-defined]


def stage_inputs(*, owner: str, project_id: str,
                  occurrence_ids: Sequence[str],
                  workdir: str = "") -> Staging:
    """Resolve every occurrence id against `owner`'s store and materialise
    it into a bounded staging directory, or raise — never returns a
    `Staging` with a partial `input_paths`.

    Raises `src.artifact_identity.ArtifactNotFound` / `NotTheOwner` exactly
    as `for_owner()` does (CONTRATO.md rule 3: "no es tuyo" y "no existe"
    responden igual), so a route calling this needs no extra branch to keep
    that guarantee.
    """
    from src import artifact_identity as identity

    workdir = workdir or new_staging_dir()
    os.makedirs(workdir, exist_ok=True)
    input_paths: dict = {}
    for occurrence_id in occurrence_ids:
        occ = identity.for_owner(occurrence_id, owner=owner)
        source = identity.path_for(occurrence_id, owner=owner)
        _, ext = os.path.splitext(source)
        target = os.path.join(workdir, f"{occ.id}{ext}")
        if not os.path.exists(target):
            try:
                os.link(source, target)
            except OSError:
                import shutil
                shutil.copyfile(source, target)
        input_paths[occurrence_id] = target
    return Staging(owner=owner, project_id=project_id, workdir=workdir,
                    input_paths=input_paths)
