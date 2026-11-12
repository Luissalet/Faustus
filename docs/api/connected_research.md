# Connected-data research

Deep Research offers **My connected data** alongside its existing web mode.
This uses the same `DeepResearcher` loop, source registry, background runtime,
report library, cancellation, checkpoint resume and model routing. It does not
ingest the connected apps into another store. Evidence retained with a report
stays in the existing research result and citation snapshot.

The screen selects sources and their declared filters. **Prepare research plan**
is optional: the configured research model returns a plan and, when useful, a
clarification question. The person may answer in the context field or start
directly. Each run shows its plan and a live source timeline: query, source,
retrieval lane, original-item read, ranking, result count and failures. The
timeline survives completion and checkpoint resume. Studio groups the timeline
and bibliography by app, keeping the original citation numbers. File citations
show the cited line and, when available, the excerpt index.

## Retrieval

- Internal sources reuse unified search for conversations, notes, documents,
  brain notes and project metadata. Brain notes, notes and documents use their
  existing readers for full evidence where available.
- Mail uses the existing owner-scoped IMAP transport, readonly folder selection,
  UID search and `BODY.PEEK[]`; researching a message does not mark it read.
- Calendar queries the existing owner-scoped calendar tables. Project files use
  the existing typed context-link membership, ownership checks and resolvers;
  their citations open the linked original through an owner-checked text route.
  Internal links use the configured public Faustus URL or the request origin.
- App sources come from enabled MCP servers and the existing connector sidecar.
  Only discovered, enabled search tools classified as read-only are selected.
  Required source arguments must be available from the query, filters or schema
  defaults. No disconnected app is launched to perform research.
- Text and semantic tools (or declared `mode` lanes) are fused using the existing
  reciprocal rank fusion implementation. Returned candidates also receive the
  existing BM25/hash ranking and, if explicitly configured, HTTP embeddings.
  This reranks the returned candidates; it is not semantic recall over an app's
  entire collection. Missing embeddings are explicitly marked as degraded.
  No embedding model or text model is loaded as an implicit fallback.
- Darwin discoveries, Links saved readers and Kafka documents have app-specific
  original links and a second read through their published tools. Kafka preserves
  the cited physical page. Other apps must return a usable HTTP original link
  and text in their search results; absent links or text are visible gaps.
- Source failures leave other sources available. A run with no citable evidence
  fails explicitly. This mode never starts a web search backend or falls back to
  web search when retrieval, planning or synthesis fails.

Every factual sentence of a synthesized report must carry an existing numbered
citation. The existing citation auditor checks coverage and dangling numbers.
If synthesis omits citations, the result presents cited source excerpts instead
of attributing unsupported prose to a source. Citation presence checks attribution,
not the truth of every statement. The fallback shows a short excerpt per original.

Clicking an app original from the report opens the existing App Dock beside a
chat carrying that report, when the citation points into the connected app.
External originals and apps without a dockable original remain ordinary links.
Dock failure retains a link to open the original in its own window.

## HTTP

`GET /api/research/connected-sources` returns source ids, names, text/semantic
lanes, supported filters, app ids and tool names for the caller.

`POST /api/research/connected-plan` accepts the same brief fields as start and
returns `{plan: string[], clarification: string}`. It does not retrieve data.

`POST /api/research/start` adds these optional fields:

```json
{
  "query": "What do my notes, calendar and connected apps say about this project?",
  "data_scope": "connected",
  "source_ids": ["notes", "calendar", "mcp:connector-server-id"],
  "source_filters": {"calendar": {"after": "2026-11-01", "before": "2026-12-01"}},
  "clarification": "The current project, for November",
  "max_rounds": 2
}
```

Omitting `data_scope` preserves web research. Omitting `source_ids` selects the
current catalog. Existing status and stream endpoints carry `progress.plan`
and `progress.timeline`. Connector events can include the app preset as
`app_id`. Result and result-peek include `connected_progress`;
saved reports also retain `connected_options`. Resume and incremental refresh
retain the selected scope, source filters and clarification.

`POST /api/research/{run}/open-source` with `{url}` accepts only an original
already cited by the caller's report. It returns the report chat's session id
after docking the app at the cited original. App identity and origin come from
the existing connector sidecar, not caller-supplied app metadata.

`GET /api/research/project-source?project_id=...&link_id=...&path=...` reads a
linked original after checking current project membership and source ownership.
Relative paths for folder links stay inside the linked folder.

## MCP

The built-in `connected_research` server exposes:

| Tool | Result |
| --- | --- |
| `connected_research_sources` | Available sources, lanes and filters |
| `connected_research_plan` | Plan and optional clarification |
| `connected_research_start` | Run id, using connected data only |
| `connected_research_status` | Live plan and timeline |
| `connected_research_result` | Saved report and citations, without consuming it |

It calls the same running Faustus API and uses the existing internal/bearer
authentication. `ODYSSEUS_MCP_RESEARCH_OWNER` supplies an external server's owner;
internal calls carry that identity through `X-Odysseus-Owner`. The server uses
`FAUSTUS_INTERNAL_API_URL` / `FAUSTUS_URL` and the existing token variables.

## Verification

`tests/test_connected_research.py` covers real MCP result envelopes, three-source
retrieval, source filters, text/semantic fusion, visible failures, invalid
original URLs, citation fallback, owner isolation, durable scope and the absence
of web fallback. Run it with the research runtime, resume and ownership tests.
Live evaluation receipts and screenshots belong outside the repository; they
must identify real data versus fixtures and any verification that remains blocked.
