"""Timeline domain package (WP13): rational clocks, tracks/clips, retiming,
snapping, validation and a compact projection for the Studio/exporters.

Depends only on ``src/creator/ops/model.py`` (WP12's ``Rational``,
``TimeRange``, ``RetimingMap`` — the one authority for "times as exact
integers at an explicit tick rate", per
``docs/spec/creator/plan/docs/03_ARQUITECTURA_Y_CONTRATOS.md`` §"Tiempo,
regiones y operaciones") and ``src/creator/errors.py``. Nothing here knows
about sqlite, HTTP or the document store — every function is ``(plain dict
in) -> (plain dict/dataclass out)`` so it composes with both
``src/creator/ops/timeline_ops.py`` (WP12's typed ops, extended here
additively) and read-only callers such as an exporter.
"""
from __future__ import annotations
