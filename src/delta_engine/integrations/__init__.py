"""Where the Delta Engine meets the subsystems that already had authority.

Each module here is a one-way adapter, and the direction matters more than the
code: a delta INFORMS `prove`, a ChangeSet and the State Mirror, and is
informed BY none of them. §1 is explicit -- "Delta Engine no ejecuta
correcciones, no elige por sí solo una rama y no declara completado un run" --
and the way that stays true is that nothing in this package writes to another
subsystem's source of truth.

The one rule worth stating twice, because it is the one a future reader will be
tempted to break: `assessment` and `prove.VERDICTS` are different vocabularies
that share the word `partial`, and `prove` stays the authority about the run.
`prove.py` here maps between them explicitly and only downwards.
"""

from __future__ import annotations

__all__: list = []
