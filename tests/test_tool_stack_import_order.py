"""Any module of the tool stack can be the first one imported.

`src.agent_tools` used to re-export parsing, schemas and execution eagerly at
the bottom of its `__init__`, while each of those modules imports `ToolBlock`
from the package. Entered through the package the cycle closed fine; entered
through any of them it raised ImportError on a partially initialised module.
The app always imports the package first, so it never showed there -- in the
suite it decided which tests passed by which test happened to run before, and
`tool_serve.schema_for` swallowed the error and served tools without schema.

Each case runs in a fresh interpreter: inside this process the package is
already imported and the cycle can no longer be observed.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FIRST_IMPORTS = [
    "src.tool_schemas",
    "src.tool_parsing",
    "src.tool_execution",
    "src.tool_implementations",
    "src.tool_serve",
    "src.tool_discovery",
    "src.tool_policy",
    "src.tool_security",
    "src.agent_tools",
]


@pytest.mark.parametrize("first", FIRST_IMPORTS)
def test_the_tool_stack_imports_from_any_entry_point(first):
    code = (
        f"import {first}\n"
        "from src.tool_serve import schema_for\n"
        "from src.agent_tools import FUNCTION_TOOL_SCHEMAS, parse_tool_blocks, execute_tool_block\n"
        "schema = schema_for('ask_user')\n"
        "assert schema and schema['function']['name'] == 'ask_user', schema\n"
        "assert FUNCTION_TOOL_SCHEMAS and callable(parse_tool_blocks) and callable(execute_tool_block)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
