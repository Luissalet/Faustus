import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
def _between(source, start, end):
    start_idx = source.index(start)
    end_idx = source.index(end, start_idx)
    return source[start_idx:end_idx]


def _posix_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


def test_remote_windows_stop_tree_payload_survives_shell_parsing():
    ps = (
        "function Stop-Tree([int]$Id) { "
        "Get-CimInstance Win32_Process -Filter ('ParentProcessId = ' + $Id) "
        "-ErrorAction SilentlyContinue | ForEach-Object { Stop-Tree ([int]$_.ProcessId) }; "
        "Stop-Process -Id $Id -Force -ErrorAction SilentlyContinue }; "
        "$p = Get-Content '$env:TEMP\\odysseus-sessions\\serve_abc.pid' "
        "-ErrorAction SilentlyContinue; "
        "if ($p -match '^\\d+$') { Stop-Tree ([int]$p) }"
    )
    remote_command = f'powershell -Command "{ps}"'
    shell_command = f"ssh -p 2222 winbox {_posix_quote(remote_command)}"

    argv = shlex.split(shell_command)

    assert argv == ["ssh", "-p", "2222", "winbox", remote_command]
    assert "$Id" in argv[-1]
    assert "$_.ProcessId" in argv[-1]
    assert "$env:TEMP" in argv[-1]
    assert "$p" in argv[-1]
