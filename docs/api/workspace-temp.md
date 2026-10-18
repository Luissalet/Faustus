# Native command temporary files

The agent's `bash`, `python`, and `powershell` tools inherit `TMP`, `TEMP`, and
`TMPDIR` pointing to `.faustus-tmp/<run-key>` within the active workspace. Without
a workspace, the persistent agent data directory is used. The directory is
created before dispatch and remains available to subsequent commands in the run.

The key comes from the session and the server-captured causal run identity, not
the tool call ID. Concurrent runs therefore use different directories while
commands in one run can share temporary files. Callers without a causal run use
a stable session directory; that fallback cannot distinguish overlapping calls
in the same session. Temporary contents are retained, with no automatic cleanup.

The legacy MCP routing adapter carries the captured session, owner and causal
call into its native fallback, both when no manager exists and when the manager
reports a disconnected server. PowerShell's direct dispatch carries the same
captured call. Thus Python, Bash and PowerShell share the same run directory
through the normal tool dispatcher. Connected MCP calls retain their original
tool arguments; this context is not added to remote requests.

Existing isolated workers retain their own temporary folders and package caches.
Ordinary runs keep their existing package/model caches, environment options, and
credentials. The native environment adapter continues to remove the application's
virtual environment and private variables before launching foreign code.

Use Python's `tempfile`, PowerShell's `[IO.Path]::GetTempPath()` or
`[IO.Path]::GetTempFileName()`, and Git Bash's quoted `"$TMPDIR"` or
`mktemp "$TMPDIR/name.XXXXXX"`. Native children inherit the same variables.

On Windows, Git Bash's literal `/tmp` is a separate MSYS mount. The installed
`usertemp` mount can still point to the user's global temporary directory even
when all three environment variables point to the workspace. This change does
not redirect literal `/tmp`, rewrite command text, or change shared mounts.
Commands can still explicitly select other paths. This is a temporary-directory
default, not filesystem confinement or proof that a task obeyed its folder scope.

The first implementation passed direct-handler tests but failed a native turn:
the legacy Python/Bash adapter dropped context while PowerShell retained its
session. Dispatcher tests now exercise both fallback branches with real native
processes, a shared marker across all three tools, and two distinct runs. Native
API/MCP validation of the revised adapter remains a separate verification step.
