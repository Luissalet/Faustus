# Plugins

A plugin is an application you already use, connected to Faustus. Jobhunter's
Hoard is a job-search app; Writer's Hoard is where manuscripts live; Dorian's
Hoard holds credentials and what is known about you. You run them on their
own. Faustus connects to them, the way a word processor add-in connects a
document to an assistant: the document is not part of the assistant.

So "plugin" describes a role, not a containment. Faustus never owns a
plugin's data, never speaks for it, and never removes it. What it does is
narrower and more useful: find it, talk to it, start it when it is needed,
and show it when you ask.

## One file

Everything Faustus needs to know about a plugin is in one manifest.

```
plugins/<id>/plugin.json              shipped with Faustus
<DATA_DIR>/plugins/<id>/plugin.json   installed on this machine
<app-dir>/faustus-plugin.json         shipped by the application itself
```

The third is the interesting one. The author of an application knows what it
exposes, and should be able to say so in their own repository without sending
a patch to Faustus. An application found running with a `faustus-plugin.json`
beside it is offered for connection on sight, with the form already filled
in.

A manifest under `<DATA_DIR>` with the same id as a shipped one replaces it —
that is how you patch a shipped plugin without forking — and the replacement
is reported rather than silent.

## The manifest

```jsonc
{
  "schema": 1,
  "id": "ledger",                   // folder name must match; [A-Za-z0-9-_]
  "name": "Ledger",
  "purpose": "Keep and query a household ledger.",
  "capabilities": ["accounts", "entries", "reports"],

  // Values the user supplies when connecting. A placeholder with no value
  // and no default leaves the plugin "unconfigured" rather than broken.
  "placeholders": ["LEDGER_DIR", "APP_URL"],
  "defaults": {
    "APP_URL": "http://127.0.0.1:8790",
    // %VAR%, $VAR and ~ are expanded when the manifest is read. Write the
    // variable, never the expansion: a manifest travels, and an expanded
    // path is one machine's answer to everyone's question.
    "TOKEN_FILE": "%APPDATA%/ledger/token"
  },

  "app": {
    "url_default": "http://127.0.0.1:8790",
    // The page to open when someone says "show me". null when the port is
    // an API and not a page — offering to open one of those produces a
    // blank tab and a puzzled user.
    "ui_url": "{APP_URL}",
    "health": { "path": "/api/health", "expect": { "service": "ledger" } },
    // How a scan recognises this app among everything else listening. Any
    // field your health body actually carries, plus "title" for the page
    // title. Without this, your app can never be offered automatically.
    "identify": { "service": ["ledger"], "title": ["ledger"] },
    // A documented example, never executed as written. It is what the
    // Connectors screen offers as a starting point for a launch profile.
    "launch_hint": {
      "kind": "process",
      "executable": "node",
      "argv": ["server.js"],
      "cwd": "{LEDGER_DIR}",
      "readiness": { "url": "{APP_URL}/api/health", "timeout_s": 20 }
    }
  },

  "mcp": {
    "transport": "stdio",
    "command": "node",
    "args": ["{LEDGER_DIR}/mcp.js"],
    "env": { "LEDGER_URL": "{APP_URL}" },
    // Forwarded only when the user supplies them; never required.
    "optional_env": ["LEDGER_PROFILE"]
  },

  "notes": "Anything a person reading the Connectors screen should know."
}
```

Two placeholders are always available and never need declaring:
`{FAUSTUS_DIR}` and `{FAUSTUS_PYTHON}`.

Unknown keys are refused rather than ignored. A manifest is the whole plugin,
so a key that goes nowhere is a promise that will not be kept.

## If your app has no MCP server

It does not need one. Faustus ships a REST bridge (`bridges/rest_mcp`) that
turns an OpenAPI document — or a hand-written manifest of endpoints — into
tools. Point `mcp` at it:

```jsonc
"mcp": {
  "command": "{FAUSTUS_PYTHON}",
  "args": ["{FAUSTUS_DIR}/bridges/rest_mcp/server.py"],
  "env": {
    "REST_BASE_URL": "{APP_URL}",
    "REST_OPENAPI_URL": "{APP_URL}/openapi.json",
    "REST_NAME": "Ledger"
  }
}
```

Two of the shipped plugins work this way and have no MCP server of their own.

## What Faustus can and cannot do with your app

It can **read the list** (`plugins_list`), **start** an app that is down, and
**show** it (`plugin_app`). Three limits are deliberate, and each is tested:

- **It starts only from a launch profile the user saved.** Guessing a command
  line for somebody else's application is how you run the wrong binary with
  the right name. No profile, no start — it says so instead.
- **An app that is already answering is adopted, never restarted.** The user
  started it, and it may have their work open.
- **Nothing can stop an app.** Stopping stays a button a person presses. An
  agent that can stop an app it did not start can close the document you are
  writing in.

## Writing one for your own project

1. Add `faustus-plugin.json` to your repository root.
2. Make your app answer something identifying — a `/api/health` with a
   `service` field is enough, and a distinctive `<title>` works too.
3. Run your app. The Connectors screen offers it under nearby apps, with
   `{YOUR_DIR}` and `APP_URL` already filled from what was found.

Adopting it copies the manifest into `<DATA_DIR>/plugins/`. That is a
snapshot, not a link: a later version of your app that offers something
different is a change the user sees and accepts, not one that rewrites a
live connection underneath them.

## Checking one

```
python -c "from src import plugins; print(plugins.read_app_manifest(r'<app-dir>'))"
```

`None` means it was refused and the reason is logged.
`plugins.load_all().errors` lists every manifest that did not load on this
installation and why — which is what the screen shows, rather than quietly
having fewer plugins than you thought.
