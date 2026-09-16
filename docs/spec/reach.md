# Reach (R1) — eyes on the internet

Idea from Agent-Reach (`Panniantong/agent-reach`), reworked for Faustus:
instead of shelling out to third-party CLIs (`twitter-cli`, `bili-cli`,
OpenCLI...), each channel is a small Python module with an **ordered list of
backends** that Faustus tries **for real** (the actual call, never just a
`which`/import check) and falls back automatically when one fails — every
attempt is recorded on the result so the caller sees exactly which backend
answered and why the others didn't. No third-party CLI is required for any
channel to work; `gh` and `feedparser` are used opportunistically when
present.

## Layout (`src/reach/`)

- `base.py` — `Channel`, `Backend`, `Availability`, `ReachResult`,
  `ReachBackendError`. `Backend.available(live=False)` is the doctor-facing
  cheap probe, cached 10 minutes per `(backend class, live)` key —
  `live=False` only inspects config/optional deps (no network); `live=True`
  makes one real cheap request. `Channel.read`/`Channel.search` iterate
  `backends` in order, catch each failure, and stamp `attempts` (backend
  name, ok/fail, reason) plus `channel`/`backend`/`fetched_at` on the
  winning result.
- `http_client.py` — single `make_client()` seam so every backend's HTTP
  calls can be redirected to `httpx.MockTransport` in tests without network.
- `credentials.py` — `reach_<channel>_token` settings, decrypted through
  `src.secret_storage` (same `enc:`-prefix convention as
  `oidc_client_secret`), never logged or returned by any tool/route.
- One module per channel: `web.py`, `youtube.py`, `github.py`, `reddit.py`,
  `x.py`, `hackernews.py`, `rss.py`, `arxiv.py`, `wikipedia.py`.
- `router.py` — `detect_channel(url_or_query)` (domain/pattern match),
  `read(url, channel=None, **kwargs)`, `search(query, channels=None,
  limit=10, **kwargs)` (defaults to `["web"]`).
- `doctor.py` — `doctor(live=False)`: per-channel/backend status
  (`ready`/`needs_config`/`unavailable` + reason + active backend), "`N/M`
  channels ready" summary. Never includes a token value.

## Channels and backend order

| Channel | Backends (in order) |
|---|---|
| `web` | Faustus's existing `web_fetch` (`services.search.content.fetch_webpage_content`, reused, not reimplemented) → Jina Reader (`r.jina.ai`, on by default, `reach_jina_enabled`/`reach_jina_api_key`) → an active browser session (see below) |
| `youtube` | Faustus's existing `services/youtube/youtube_handler.py` (transcript via `youtube_transcript_api` + top comments via `yt-dlp`, reused) → `youtube_transcript_api` called directly (covers `init_youtube()` failing independently of the package being installed) → Jina Reader on the watch page |
| `github` | Public REST API (`reach_github_token` optional) → `gh` CLI if `shutil.which("gh")` → Jina Reader |
| `reddit` | Public `.json` endpoints (thread w/ comments, subreddit/search listings, explicit User-Agent) → `old.reddit.com` via Jina → browser session |
| `x` | `api.fxtwitter.com` → `api.vxtwitter.com` → `cdn.syndication.twimg.com/tweet-result` → Jina Reader → browser session. **Search** only via browser session or a configured `reach_nitter_base` — with neither, reports `unavailable` with that reason rather than inventing results. |
| `hackernews` | Algolia HN Search API (search + item w/ comments flattened) |
| `rss` | `feedparser` if installed → minimal RSS 2.0 / Atom parser over `xml.etree` (no hard dependency) |
| `arxiv` | Public arXiv Atom API (search + abstract by id) |
| `wikipedia` | REST summary API + MediaWiki search action, language-aware |

## Tools & routes

- `reach_read` — `{url, channel?}` → `ReachResult` as Markdown-ish text plus
  `channel`/`backend`/`source_trust`/`truncated`/`attempts`.
- `reach_search` — `{query, channels?, limit?}` → per-channel hit list.
- `reach_doctor` — `{live?}` → health report.
- `routes/reach_routes.py`: `GET /api/reach/doctor?live=1`,
  `POST /api/reach/read`, `POST /api/reach/search` — admin-gated
  (`core.middleware.require_admin`), same posture as
  `routes/external_runtimes_routes.py`'s config surface.

`web_fetch` (`src/agent_tools/web_tools.py`) is untouched — Reach's Jina
fallback lives entirely inside `src/reach/web.py`; the `web` channel calls
into `fetch_webpage_content` (already reused, not duplicated) first and only
reaches for Jina when that fails.

## Settings added (`src/settings.py`)

`reach_github_token`, `reach_reddit_token`, `reach_x_token`,
`reach_jina_enabled` (default `True`), `reach_jina_api_key`,
`reach_nitter_base`. All `reach_*`-prefixed, so out of scope for
`agent_settings_schema.py`'s `agent_*`/`browser_*`/`desktop_*` parity check.

## What's half-built, declared honestly

The **browser-session backend** (`web.BrowserSessionBackend`, reused by
`reddit`/`x`) is a real, wired seam — it looks for a
`kwargs["ctx"]["browser_session_reader"]` async callable and degrades to a
clear `unavailable`/`ReachBackendError` when none is supplied — but nothing
in this lote drives an actual Faustus browser session synchronously from
Python: Faustus's browser automation runs through the agent's own MCP
tool-call loop, not a function this module can call directly. Wiring a real
reader (e.g. from `src/browser_view.py` state once a session exists) is
future work; `reach_doctor` reports it as `needs_config` with that reason
rather than pretending it works.

## Tests

`tests/test_reach_base.py` (Channel/Backend fallback + attempts + 10-minute
availability cache), `tests/test_reach_router.py` (`detect_channel`),
`tests/test_reach_backends.py` (one real-fallback case per channel over
`httpx.MockTransport`, `youtube_handler`/`youtube_transcript_api`/`gh`/
`feedparser` all monkeypatched — no network), `tests/test_reach_doctor.py`
(no-network + mocked `live=True` + token-never-in-output),
`tests/test_reach_tools.py` (tool registration + execution),
`tests/test_reach_credentials.py` (encrypt/decrypt round-trip, redaction),
`tests/test_reach_routes.py` (admin gate, request validation).
