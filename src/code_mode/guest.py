"""Code Mode guest bootstrap (T6, A10/A11).

Runs standalone inside the isolated ``python -I`` subprocess ``runner.py``
launches -- stdlib only, no ``src.*`` import (the child has no PYTHONPATH
and no Faustus source on it by design). Exposes the ``tools`` object the
generated code calls (``tools.call(name, args)``, ``tools.list()``), talks
to the parent over stdin/stdout as line-delimited JSON, and self-enforces
the call-count and output-size quotas the parent also enforces from the
outside (defense in depth: the parent kills the process either way, but a
cooperative script exits cleanly with its own diagnostic reason).

Protocol (one JSON object per line):
  child -> parent : {"call_id", "type": "call", "tool", "args"}
                     {"call_id", "type": "list"}
                     {"type": "final", "status", "error", "output",
                      "calls_made", "last_call"}
  parent -> child : first line: {"max_calls", "max_output_bytes"} (config)
                     then, per request: {"call_id", "ok", "result"|"error"}
"""

from __future__ import annotations

import json
import os
import sys
import traceback


class _CallQuotaExceeded(Exception):
    pass


class _OutputQuotaExceeded(Exception):
    pass


class _ToolBridgeError(Exception):
    pass


class _CapturedOutput:
    """Stand-in for sys.stdout/sys.stderr inside the guest: captures the
    generated code's own print()/traceback output, capped at
    ``max_output_bytes``. Raises once the cap is exceeded rather than
    silently truncating, so a runaway ``print`` loop terminates the guest
    promptly instead of growing the capture buffer without bound."""

    def __init__(self, limit):
        self.limit = int(limit) if limit else None
        self._parts: list[str] = []
        self.size = 0

    def write(self, s):
        text = "" if s is None else str(s)
        if not text:
            return 0
        if self.limit is not None:
            grew = len(text.encode("utf-8", "replace"))
            if self.size + grew > self.limit:
                remaining = max(0, self.limit - self.size)
                if remaining:
                    self._parts.append(text[:remaining])
                    self.size += remaining
                raise _OutputQuotaExceeded(
                    f"Code Mode output quota exceeded ({self.limit} bytes)."
                )
            self.size += grew
        self._parts.append(text)
        return len(text)

    def flush(self):
        pass

    def getvalue(self) -> str:
        return "".join(self._parts)


class _ProtocolChannel:
    """The JSON-lines link to the parent: writes go through a DUPLICATED
    stdout fd (so redirecting ``sys.stdout`` for the guest code's own
    prints, right after this is constructed, never touches this channel);
    reads come from the real stdin, which the guest code has no reason to
    touch."""

    def __init__(self):
        self._out = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8", newline="\n")
        self._in = sys.stdin

    def send(self, msg: dict) -> None:
        self._out.write(json.dumps(msg, default=str) + "\n")
        self._out.flush()

    def recv(self) -> dict:
        line = self._in.readline()
        if not line:
            raise _ToolBridgeError("Code Mode bridge closed unexpectedly.")
        try:
            return json.loads(line)
        except Exception as e:  # noqa: BLE001
            raise _ToolBridgeError(f"Malformed bridge response: {e}") from e

    def close(self) -> None:
        try:
            self._out.close()
        except Exception:
            pass


class Tools:
    """The object generated code sees as ``tools``."""

    def __init__(self, channel: _ProtocolChannel, max_calls):
        self._channel = channel
        self._max_calls = int(max_calls) if max_calls else None
        self.calls_made = 0
        self.last_call = None

    def call(self, name: str, args: dict | None = None):
        if self._max_calls is not None and self.calls_made >= self._max_calls:
            raise _CallQuotaExceeded(
                f"Code Mode call quota exceeded ({self._max_calls})."
            )
        self.calls_made += 1
        self.last_call = name
        call_id = f"call_{self.calls_made}"
        self._channel.send({"call_id": call_id, "type": "call", "tool": name, "args": args or {}})
        resp = self._channel.recv()
        if resp.get("call_id") != call_id:
            raise _ToolBridgeError("Code Mode bridge response out of order.")
        if not resp.get("ok", False):
            return {"error": resp.get("error") or "tool call failed", "exit_code": 1}
        return resp.get("result")

    def list(self, detail: str = "catalog"):
        call_id = f"list_{self.calls_made}"
        self._channel.send({"call_id": call_id, "type": "list", "detail": detail})
        resp = self._channel.recv()
        if resp.get("call_id") != call_id:
            raise _ToolBridgeError("Code Mode bridge response out of order.")
        return resp.get("result") or []


def _install_cpu_guard():
    """Best-effort: turn SIGXCPU (RLIMIT_CPU, set by the parent's
    preexec_fn on POSIX) into a catchable Python exception so the guest can
    still send its diagnostic ``final`` message instead of dying silently.
    A no-op on Windows (no SIGXCPU) or if signal handling is unavailable."""
    try:
        import signal

        class _CpuQuotaExceeded(Exception):
            pass

        def _on_xcpu(_signum, _frame):
            raise _CpuQuotaExceeded("Code Mode CPU quota exceeded.")

        signal.signal(signal.SIGXCPU, _on_xcpu)
        return _CpuQuotaExceeded
    except Exception:
        return None


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: guest.py <user_code_path>\n")
        return 2
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        user_code = f.read()

    channel = _ProtocolChannel()

    config_line = sys.stdin.readline()
    try:
        config = json.loads(config_line) if config_line.strip() else {}
    except Exception:  # noqa: BLE001
        config = {}
    max_calls = config.get("max_calls")
    max_output_bytes = config.get("max_output_bytes")

    cpu_quota_exc = _install_cpu_guard()

    captured = _CapturedOutput(max_output_bytes)
    sys.stdout = captured
    sys.stderr = captured

    tools = Tools(channel, max_calls)

    status = "ok"
    error_text = None
    try:
        exec(  # noqa: S102 - this IS the isolated Code Mode execution
            compile(user_code, "<code_mode>", "exec"),
            {"__name__": "__main__", "tools": tools},
        )
    except _OutputQuotaExceeded as e:
        status = "output"
        error_text = str(e)
    except _CallQuotaExceeded as e:
        status = "max_calls"
        error_text = str(e)
    except MemoryError as e:  # RLIMIT_AS tripped (POSIX)
        status = "memory"
        error_text = "MemoryError"
    except SystemExit:
        status = "ok"
    except BaseException as e:  # noqa: BLE001 - report every failure, never crash silently
        if cpu_quota_exc is not None and isinstance(e, cpu_quota_exc):
            status = "cpu"
            error_text = str(e)
        else:
            status = "error"
            error_text = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

    try:
        channel.send({
            "type": "final",
            "status": status,
            "error": error_text,
            "output": captured.getvalue(),
            "output_bytes": captured.size,
            "calls_made": tools.calls_made,
            "last_call": tools.last_call,
        })
    except Exception:  # noqa: BLE001 - the parent will time the child out either way
        pass
    channel.close()
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
