"""Harness refinement: after a task, PROPOSE the smallest edit to the harness.

The harness around the immutable base prompt has four editable surfaces:
the project's standing instructions, a skill, a memory entry and a sub-agent
spec. After a task that shows a real problem (the user corrected the
assistant, said the same thing twice, a tool failed and was repeated), a local
model may propose ONE small edit to ONE of them. The proposal is stored as
``pending``; a person approves it, rejects it, or undoes it later by id.

* :mod:`~src.harness_refinement.store`    the records, the status machine, undo, the trigger -> result log
* :mod:`~src.harness_refinement.targets`  the four axes: read, validate, write, restore
* :mod:`~src.harness_refinement.signals`  trajectory and the deterministic pre-filter
* :mod:`~src.harness_refinement.proposer` the model call and the validation of its answer
* :mod:`~src.harness_refinement.runner`   the background queue behind ``harness_refinement_enabled``

Nothing in this package applies an edit on its own: the only writer of a
target is :func:`store.approve`, reached from the routes behind ``require_human``.
"""
from src.harness_refinement.store import AXES, OPS, STATUSES, HarnessError  # noqa: F401

__all__ = ["AXES", "OPS", "STATUSES", "HarnessError"]
