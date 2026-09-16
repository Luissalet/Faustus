"""tests/creator_harness — WP36: adapter/failure harness for Creator.

Everything under this package is TEST INFRASTRUCTURE, not application code:
deterministic fixture generators (`fixtures.py`), configurable fake engines
that stand in for a real ComfyUI/ffmpeg backend to exercise failure paths a
real engine will not reliably reproduce on demand (`fake_engines.py`), the
minimum failure matrix from `docs/spec/creator/plan/docs/08_PRUEBAS_Y_ACEPTACION.md`
parametrized against every adapter `src.creator.adapters.registry()` finds
(`matrix.py`), and the tests themselves.

Nothing here edits `src/creator/adapter_port.py` or any adapter under
`src/creator/adapters/`. A bug this harness finds in a real adapter is
recorded as `xfail(strict=True)` with a reason and reported, not patched —
CONTRATO.md rule 8 / the WP36 ficha: "NO arregles adaptadores".

See `docs/spec/creator/WP36.md` for what this package covers, its evidence
levels, and its known gaps.
"""
from __future__ import annotations
