# UI checks and project startup

Faustus automatically checks changed web interfaces through HTTP and, when
available, its browser audit. It detects Flask, FastAPI, npm start/dev and
static HTML. A static preview serves files and does not provide an application
backend.

Projects with another startup command can declare it in
`.faustus/ui_smoke.json`. The file takes precedence over automatic detection:

```json
{
  "argv": ["{python}", "app.py", "--port", "{port}", "--db", "{data_dir}/test.sqlite3"],
  "cwd": ".",
  "entry_path": "/"
}
```

Use the actual arguments supported by the application. Faustus does not guess
flags from a filename or README. `argv` is a nonempty array of strings; its
first element is a nonempty executable. Empty later arguments are preserved.
The optional `cwd` names an existing directory relative to the workspace and
within it; the default is the workspace. `entry_path` defaults to `/` and must
be a local absolute URL path. Queries and fragments are allowed; another
server or a backslash in the URL path is not.

Only these literal markers are substituted, once, in the original arguments:

| Marker | Value |
|---|---|
| `{python}` | The existing project Python resolver's executable |
| `{port}` | The port allocated for this check |
| `{workspace}` | The project workspace |
| `{data_dir}` | A new temporary directory for this check |

Arguments retain their boundaries, spaces, Unicode and quotes. Unknown braces,
JSON literals and shell operators remain literal arguments; this does not add
a shell. The existing native environment and `PORT` handling are retained.

The temporary directory is created only when execution uses `{data_dir}`.
The declared command must explicitly direct disposable data there; the marker
does not isolate an application's implicit or hardcoded database. Faustus does
not copy project databases or adopt another running server. It stops its own
child tree after the check and removes temporary data after observed process
termination. An uncertain stop or cleanup error retains the directory and a
diagnostic, without hiding the check's original result.

An invalid declaration is a failed check with a configuration diagnostic, not
a silent fallback to a static preview. A change to only this file is recognised
by the normal UI check flow. The file can be written through the existing
project file tools and their API/MCP entry points; no separate runner is needed.

The declared server's entry is used by both the HTTP crawl and browser audit.
Readiness alone is not verification: missing resources, incorrect MIME types
and browser console errors still fail. Reports preserve the actual command,
argument list, working directory, entry and temporary cleanup state. No
environment dump is included. Existing enablement, timeouts, automatic
detection and audit capabilities remain available.
