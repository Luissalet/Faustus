"""Code Mode (T6, A10/A11): the model writes a short Python program that
composes several Faustus tool calls in one round instead of N model round
trips.

- ``bridge.py``: the Faustus-side dispatcher. A guest call is turned into
  the exact same ``ToolBlock`` + ``execute_tool_block`` call an ordinary
  model tool call would produce, so a destructive/disabled tool gets the
  same rejection either way (A10).
- ``runner.py``: launches the isolated subprocess (``python -I``, minimal
  env, temp cwd, no network access granted) and enforces wall time / call
  count / output size / CPU / memory quotas, killing the process tree and
  returning a diagnostic receipt when one is hit (A11).
- ``guest.py``: the small script injected into the subprocess. Defines the
  ``tools`` object the generated code calls (``tools.call(name, args)``,
  ``tools.list()``), talking to the parent over stdin/stdout as
  ``{"call_id", "tool", "args"}`` -> ``{"call_id", "ok", "result"|"error"}``
  JSON lines.
"""

from src.code_mode.runner import run_code_mode

__all__ = ["run_code_mode"]
