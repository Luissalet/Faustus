"""QA-16 · Dos agentes escritores (docs/spec/v2/acceptance_scenarios.json).

Estimulo: dos hijos intentan editar el mismo archivo.
Resultado exigido (literal): "Ownership/lease o reconciliacion; no
last-writer-wins silencioso."

Requisitos: PLAN-03.

Estado: verde. `src/agent_tools/subagent_tools.py::FileLockRegistry`
(PLAN-03 parcial segun MAPA_REUTILIZACION - "backend reparte permisos/locks
por delegacion") ya reserva ficheros por worker: `claim()` da propiedad,
`blocked_by()` dice quien mas quiere el mismo fichero, y
`write_block_reason()` rechaza una escritura de un worker que no es el
dueno en vez de dejar que el ultimo en escribir gane en silencio.
"""
import json

import pytest

from src.agent_tools import subagent_tools as st

pytestmark = pytest.mark.qa_state("green")


def test_two_children_editing_the_same_file_get_ownership_not_silent_overwrite(tmp_path):
    reg = st.FileLockRegistry(str(tmp_path))

    # Worker A claims src/shared.py first - it becomes the owner/lease holder.
    reg.claim("A", ["src/shared.py"])
    assert reg.blocked_by("B", ["src/shared.py"]) == "A"

    # Worker B tries to write the same file while A still owns it: refused
    # with a reason naming the file, not a silent last-writer-wins.
    async def _as_b():
        st._LOCK_CTX.set(st._LockGuard(reg, "B"))
        try:
            return st.write_block_reason(
                "edit_file", json.dumps({"path": "src/shared.py", "old_string": "x", "new_string": "y"}))
        finally:
            st._LOCK_CTX.set(None)
    import asyncio
    reason = asyncio.run(_as_b())
    assert reason is not None
    assert "src/shared.py" in reason

    # A's own write to the file it owns is never blocked by its own lease.
    async def _as_a():
        st._LOCK_CTX.set(st._LockGuard(reg, "A"))
        try:
            return st.write_block_reason(
                "edit_file", json.dumps({"path": "src/shared.py", "old_string": "x", "new_string": "y"}))
        finally:
            st._LOCK_CTX.set(None)
    assert asyncio.run(_as_a()) is None

    # Only after A releases does the file become available to B (an explicit
    # reconciliation step, not an implicit race).
    assert reg.release("A") == ["src/shared.py"]
    assert reg.blocked_by("B", ["src/shared.py"]) is None
