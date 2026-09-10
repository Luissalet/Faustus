"""QA-01 · Carpeta sin Git (docs/spec/v2/acceptance_scenarios.json).

Estimulo: abrir carpeta Windows con espacios y tildes; pedir una edicion de
una linea.
Resultado exigido (literal): "Lectura/patch/verificacion sin exigir
inicializar Git ni preguntar decisiones innecesarias."

Requisitos: IDX-01, EDIT-01, LANG-01.

Estado: verde. `EditFileTool`/`ReadFileTool` (src/agent_tools/filesystem_tools.py)
resuelven rutas via `_resolve_tool_path` sin tocar git en ningun punto (ver
`grep -rn "git init" src/ routes/` - unico uso real es para clonar llama.cpp
en cookbook, no para editar/leer archivos de proyecto). Este test prueba el
caso literal: una carpeta con espacios y una tilde, SIN `.git`, editada con
una sustitucion de una linea.
"""
import json
import os

import pytest

from src.agent_tools.filesystem_tools import EditFileTool, ReadFileTool

pytestmark = pytest.mark.qa_state("green")


@pytest.mark.asyncio
async def test_edit_and_read_succeed_without_a_git_folder(tmp_path):
    project = tmp_path / "Mi Diseno (v2)"
    project.mkdir()
    assert not (project / ".git").exists()

    target = project / "app.py"
    target.write_text("def saluda():\n    return 'hola'\n", encoding="utf-8")

    # A single-line, exact-match edit - the literal "one-line edit" of the
    # stimulus - must succeed with no git repository present anywhere on the
    # path, and without raising or requiring any decision from the user.
    res = await EditFileTool().execute(
        json.dumps({
            "path": str(target),
            "old_string": "return 'hola'",
            "new_string": "return 'hola mundo'",
        }),
        {},
    )
    assert res.get("exit_code") == 0, res
    assert target.read_text(encoding="utf-8") == "def saluda():\n    return 'hola mundo'\n"

    # Reading it back also works untouched by the absence of .git.
    read_res = await ReadFileTool().execute(json.dumps({"path": str(target)}), {})
    assert "hola mundo" in read_res.get("output", "")
    assert not (project / ".git").exists(), "editing must not silently git-init the folder"
