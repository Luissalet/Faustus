# Data destinations

Open **Settings → Data destinations** to read or download the current
configuration inventory. The report shows model endpoints and the saved
conversations and projects associated with them, configured MCP and HTTP
integrations, messaging and mail, web search, calendar sync, voice and retrieval.
It distinguishes this machine, another machine on the network, external services
and destinations that cannot be determined from configuration.

Read the same report with the built-in MCP tool `data_boundary_report` (server
`data_boundary`), or from the installation's terminal:

```console
python -m src.data_boundary_report
```

The command uses `FAUSTUS_INTERNAL_API_URL`, then `FAUSTUS_URL`, then the local
API. Use `--url` to select an installation. Standalone MCP clients can launch
`python -m mcp_servers.data_boundary_server`. Both reuse the installation's
internal-tool authentication (`FAUSTUS_INTERNAL_TOOL_TOKEN`) when required.

- `GET /api/privacy/report` uses the existing administrator access to privacy
  settings. It returns a versioned JSON report with `routes`, `projects`, local
  storage notes, limits and unreadable configuration sources. Addresses show
  hosts and ports. The inventory reads configuration and conversation/project
  metadata. It makes no requests to the listed destinations.
- `GET /api/privacy/attachment-destination?endpoint_id=…&session_id=…` returns the
  address, scope and advisory warning for the active destination. Studio looks
  it up only while the composer has attachments. Sending remains available.
  A room without a single model destination gets a general notice.

Saved conversations show their saved model route. A particular turn can use
other routes for vision, tools, fallback or research. A configured endpoint can
be offline or forward requests beyond its visible address. External folder sync
and other applications on the machine can act independently of Faustus.
