"""QA-12 · Cancelar proceso (docs/spec/v2/acceptance_scenarios.json).

Estimulo: comando genera hijos mientras el usuario cancela.
Resultado exigido (literal): "UI responde, arbol propio termina; otros
procesos del equipo siguen vivos."

Requisitos: EXEC-03, TASK-04.

Estado: verde. `src/process_ownership.py::terminate_tree` ya cubre este caso
(existente segun docs/spec/v2/MAPA_REUTILIZACION.md EXEC-03, probado con un
arbol simulado en tests/test_process_ownership.py /
tests/test_process_tree_ownership.py). Este test lo prueba contra procesos
del sistema operativo REALES en lugar de un `fake_os`: lanza un padre con un
hijo real, cancela con `terminate_tree`, y comprueba que ambos mueren
mientras un tercer proceso ajeno (que representa "otros procesos del equipo")
sigue vivo.
"""
import os
import subprocess
import sys
import time

import pytest

from src import process_ownership as po

pytestmark = pytest.mark.qa_state("green")

if sys.platform == "win32":
    pytest.skip("arbol de procesos POSIX real; en Windows lo cubre el fake_os "
                "de tests/test_process_tree_ownership.py", allow_module_level=True)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_cancel_kills_own_tree_but_spares_an_unrelated_process():
    # "otros procesos del equipo": an unrelated bystander this cancel must
    # never touch.
    bystander = subprocess.Popen(["sleep", "30"])
    try:
        # The command that generates children while the user cancels: a
        # parent shell that spawns a real grandchild sleeper.
        parent = subprocess.Popen(
            ["sh", "-c", "sleep 30 & child=$!; wait $child"],
            start_new_session=True,
        )
        time.sleep(0.3)  # let the shell fork its child
        spawned_at = po.creation_time(parent.pid)
        assert spawned_at is not None

        outcome = po.terminate_tree(parent.pid, spawned_at=spawned_at,
                                     pgid=os.getpgid(parent.pid))
        assert outcome.owned, outcome  # cancel actually acted, not a silent no-op

        parent.wait(timeout=5)
        time.sleep(0.2)
        assert not _alive(parent.pid), "the cancelled root must be gone"

        # The bystander - "otros procesos del equipo" - is unaffected.
        assert bystander.poll() is None, "cancel must not touch unrelated processes"
    finally:
        bystander.kill()
        bystander.wait(timeout=5)
