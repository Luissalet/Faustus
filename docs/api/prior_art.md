# Prior art: reuse, adapt, or write (GIT-08)

Before building something, an agent should know which parts of the idea
already exist as maintained open-source projects (`reuse` — a dependency),
which exist only as reference implementations worth studying (`adapt` — copy
the approach, never the code), and which are small or generic enough to just
write. Models recall repository names badly — they invent plausible-looking
ones or cite dead projects — so this feature never lets a repository name
reach the user unverified: every `owner/name` a slate names is checked live
against the GitHub API before it is reported back.

The model does the thinking (`rubric` hands it a decomposition checklist,
nothing else); Faustus does the verification. Implementation:
`src/prior_art.py` (`rubric`, `verify`, `search`, `save_report`/`report`/
`reports`). No code was copied from any existing "prior art" tool — this is
an original implementation for Faustus.

Three surfaces expose the same functions: the HTTP routes
(`routes/prior_art_routes.py`), the built-in MCP server `prior_art`
(`mcp_servers/prior_art_server.py`), and the agent tool `prior_art`
(`src/agent_tools/prior_art_tools.py`).

## The three functions

### `rubric(idea, *, stack="", license="", constraints="")`

No network call, no model call — a checklist. Returns:

- `instructions`: English text telling the agent to split `idea` into 3–10
  components, pick exactly one verdict per component (`reuse` / `adapt` /
  `write`), list 1–3 candidate `owner/name` repos for every `reuse`/`adapt`
  component with a one-line rationale, then call `verify` — and a final line
  telling it to answer the *user* in the user's own language (the
  instructions themselves are the agent's English working notes, not a
  message to relay).
- `slate_shape`: the exact JSON shape `verify` expects.
- `verdicts`: `["reuse", "adapt", "write"]`.

Verdict guidance baked into the instructions:

| Verdict | When |
| --- | --- |
| `reuse` | Correctness is hard-won: parsers, crypto, protocols, numerical algorithms, database engines, format codecs, compression, date/timezone handling. |
| `adapt` | The best candidate's license is incompatible (mandatory downgrade path), or a project solved the same shape of problem but nothing fits as a dependency. Copy the approach, never the code. |
| `write` | Small, generic, glue code, or a dependency would cost more (API surface, transitive deps, security surface) than it saves. |

### `verify(slate, *, target_license="", stack="", owner="", project_id="")`

`slate = {idea?, components: [{name, verdict, repos: [str], rationale}]}`.

For every distinct `owner/name` across all components (deduped, capped at
40): `GET https://api.github.com/repos/{owner}/{name}`, cache-first (24h
SQLite cache, `DATA_DIR/prior_art.sqlite3`), fetched in parallel
(`ThreadPoolExecutor`, 8 workers, `httpx.Client`, an 8s total budget,
redirects followed so a renamed repo's canonical `full_name` is reported).
Never raises: a request that fails (network error, timeout, non-200) comes
back as that repo's own `unverified`/`rate_limited`/`missing` status, not an
exception.

Each repo is classified:

- **status**: `found` / `archived` / `fork` / `missing` (404) /
  `rate_limited` (403 with `X-RateLimit-Remaining: 0`) / `unverified`
  (network error, timeout, or a non-200/404/403 response) / `invalid`
  (malformed `owner/name`).
- **health** (from `pushed_at`): `active` (<6 months), `slowing` (6–18
  months), `stale` (>18 months), `unknown` (no `pushed_at`).
- **license**: the repo's SPDX id and its category (see the matrix below),
  plus a `reuse`/`adapt`/`write`-specific compatibility note against
  `target_license`.
- stars, open issues, description (truncated to 280 chars), default branch,
  homepage, `html_url`.

Repos within a component are ranked (verified + active + license-compatible
first) and the component gets one outcome:

| Outcome | Meaning |
| --- | --- |
| `confirmed` | The top candidate supports the verdict as given. |
| `downgrade` | `reuse` only — the top viable candidate is archived, stale, or its license is flagged; the report says downgrade to `adapt` (study it, write your own code) or search for a healthier alternative. |
| `replace` | Every candidate is missing (404) — nothing to reuse or study; the report suggests `search` or `write`. |
| `unverified` | The feature is disabled (`prior_art_enabled=false`) — nothing was checked at all; every named repo comes back `unverified` with that reason. |

The call returns the enriched slate, a `next_actions` list, a rendered
markdown table (`component \| verdict \| repo \| status \| license \| last
push \| ★`), `verified` (did the GitHub API answer at all — a confirmed 404
counts, only a network-level failure or the feature being disabled does
not), `rate_limited`, and a saved report `id` (`PA-000123`).

### `search(query, *, language="", limit=8, include_stale=False)`

`GET /search/repositories`, sorted by stars, with a `pushed:>` filter for
the last 2 years unless `include_stale`. For when the model has no candidate
at all — results carry the same verified fields `verify` produces, since
they come from the same API, so any of them can go straight into a slate's
`repos`.

### Reports

`save_report`/`report(id)`/`reports(limit, owner="")` persist and read back
`verify`'s evidence in `DATA_DIR/prior_art.sqlite3` (`reports` table, ids
`PA-000001`, `PA-000002`, …) so it survives past the turn that produced it.

## License compatibility matrix

SPDX ids are normalized (`-only`/`-or-later` suffix stripped, matched
case-insensitively) into four categories:

| Category | Examples |
| --- | --- |
| `permissive` | MIT, BSD-2/3/4-Clause, Apache-2.0, ISC, Zlib, Unlicense, 0BSD, CC0-1.0, WTFPL, BSL-1.0 |
| `weak_copyleft` | LGPL-2.0/2.1/3.0, MPL-1.1/2.0, EPL-1.0/2.0, CDDL-1.0/1.1 |
| `strong_copyleft` | GPL-1.0/2.0/3.0, AGPL-1.0/3.0 |
| `none` | No license, GitHub's `NOASSERTION`, or anything unrecognized — treated as default copyright ("all rights reserved"): never safe to copy from. |

Compatibility (`license_compatibility(target_category, candidate_category,
verdict)`):

- **`adapt`**: always compatible — no code is copied, so the source's
  license never attaches to yours. The note is stronger when the candidate
  has no license: read it for understanding, never copy text or code.
- **`write`**: always compatible — no dependency, no reference copy.
- **`reuse`**: the candidate becomes a dependency, so its license terms
  apply to the combined work.
  - `permissive` candidate → always compatible.
  - `none` candidate → **flagged**, not compatible: no license found means
    it is not safe to depend on at all.
  - `weak_copyleft` candidate → compatible unless the target project is
    itself `strong_copyleft`-only in the wrong direction (flagged only when
    the target is `strong_copyleft`).
  - `strong_copyleft` candidate → **flagged** unless the target project is
    already `strong_copyleft` — a GPL/AGPL dependency of a permissive or
    weak-copyleft project typically obligates the whole combined work under
    the same terms.

## Network policy and rate limits

This feature hits exactly one host, `api.github.com`, and only for
repository metadata and search — read-only, GET-only. Nothing else leaves
the machine: no repository is cloned, no credential beyond the optional
token is sent anywhere.

- **Off switch**: setting `prior_art_enabled` (default `true`). When false,
  `verify`/`search` skip the network entirely and return `verified: false`
  with the reason `"prior_art is disabled (setting prior_art_enabled=false)"`
  — never a silent empty result, `exit_code: 0` either way (the tool call
  itself succeeded; the *feature* is off).
- **Offline / real network failure**: every repo comes back `unverified`
  with its own reason (timeout, connection error, non-200 response);
  `verified: false` at the top level with `"no repository could be reached"`
  only when the GitHub API answered *none* of the requests (a mix of real
  hits and misses still reports `verified: true`).
- **Token**: optional. Read from the settings secret `prior_art_github_token`
  first (same convention as `brave_api_key`/`tavily_api_key` — see
  `src/settings.py`), else the environment (`GITHUB_TOKEN`, then
  `GH_TOKEN`). Sent only as the `Authorization` header of a request to
  `api.github.com`; never logged, never included in a report, a rendered
  table, or any tool result.
- **Unauthenticated works** (GitHub's public 60 req/h limit). A 403 response
  with `X-RateLimit-Remaining: 0` is classified `rate_limited` per repo
  (`rate_limited: true` at the top level too) rather than a generic error,
  and `X-RateLimit-Reset` is passed through when GitHub sends it.
- **Cache**: repo metadata (found, archived, forked, or a confirmed 404) is
  cached for 24h in `DATA_DIR/prior_art.sqlite3` (`repo_cache` table), so a
  repeated `verify` over overlapping slates costs no extra quota. Rate
  limits and network errors are never cached, since they are not durable
  facts about the repo.

## Tool-classification (external content)

`verify`/`search` read text from the public internet (repo descriptions,
GitHub's own metadata) that Faustus did not write, so the whole `prior_art`
tool is classified the same way `web_search`/`search_hf_models` are in
`src/tool_capabilities.py`: `ToolEffect.BROKERED_NETWORK_READ` with
`ResultIntegrity.EXTERNAL_UNTRUSTED`. (`rubric` itself makes no network call
at all, but the tool is classified as a whole, matching how `web_fetch`
covers its own no-network-yet call shapes too.)

## HTTP routes

Prefix `/api/prior-art`, `require_admin` on every endpoint (same convention
as `routes/code_graph_routes.py`).

| Method | Route | Body / query | Response |
| --- | --- | --- | --- |
| `POST` | `/rubric` | `{idea, stack?, license?, constraints?}` | `rubric()`'s dict |
| `POST` | `/verify` | `{slate, target_license?, stack?}` | `verify()`'s dict |
| `POST` | `/search` | `{query, language?, limit?, include_stale?}` | `search()`'s dict |
| `GET` | `/reports?limit=` | — | `{"reports": [...]}` (id/owner/project_id/idea/created_at, newest first) |
| `GET` | `/reports/{id}` | — | the saved report in full, or `404` |

## MCP server

Built-in server id `prior_art` ("Built-in: Prior art",
`mcp_servers/prior_art_server.py`, registered in `src/builtin_mcp.py`).
Unlike `brain`/`context`/`memory` it owns no owner-scoped store — its only
outbound calls are read-only GETs to `api.github.com` — so it needs no owner
environment variable and every tool works with zero configuration:

- `prior_art_rubric` `{idea, stack?, license?, constraints?}`
- `prior_art_verify` `{slate, target_license?, stack?}`
- `prior_art_search` `{query, language?, limit?, include_stale?}`
- `prior_art_report` `{id?, limit?}`

## Agent tool

`prior_art` `{action: rubric|verify|search|report, ...}`
(`src/agent_tools/prior_art_tools.py`, registered in
`src/agent_tools/__init__.py`, `src/tool_schemas.py`, `src/tool_capabilities.py`,
`src/tool_index.py`, `src/tool_index_examples.py`). A bare string with no
leading `{` is shorthand for `{"action": "rubric", "idea": <string>}` — the
common "is there prior art for X" case needs no JSON from the model.
Tool-RAG keywords (ES/EN): "ya existe", "hay alguna librería", "reutilizar",
"no reinventes la rueda", "qué repos", "antes de construir", "prior art",
"reuse or write", "existing library", "open source alternative".
