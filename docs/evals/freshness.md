# Freshness battery

Checks that Faustus searches the web automatically for time-sensitive
questions — sports results, weather, prices, software releases, who
currently holds a role, today's news — instead of answering "I have no
access to live results" and waiting to be told to search. Other assistants
search for this class of question without being asked; this battery is the
regression guard that keeps Faustus doing the same.

## What it does

`scripts/eval_freshness.py` sends a fixed list of 16 questions to a running
Faustus instance in agent mode, one per fresh session, over
`POST /api/chat_stream`, and reads the resulting SSE stream:

- **12 time-sensitive questions**, mixed Spanish/English, about generic,
  well-known subjects (a big club's last match, the weather in a named city,
  the price of a well-known cryptocurrency, the latest version of a
  well-known open-source project, the current coach of a big club, today's
  AI news, ...). These must trigger a `web_search` tool step and must not
  produce an answer that reads like a refusal.
- **4 timeless control questions** (sort a list in Python, what is a
  derivative, explain a hash map, write a function that adds two numbers).
  These must NOT trigger a search — the freshness heuristic
  (`src/freshness.py`) over-firing on ordinary questions would be its own
  regression.

For each question the script records: whether a `web_search` step appeared,
whether the final answer text matched a refusal regex
(`no tengo acceso|no puedo acceder|I don't have access|cannot access|real-time`),
elapsed seconds, and how many sources came back on the `web_sources` event.

## Running it

```
FAUSTUS_USER=<user> FAUSTUS_PASSWORD=<password> \
  python3 scripts/eval_freshness.py --base http://127.0.0.1:7000
```

Requires a running Faustus server reachable at `--base` and a real account
(`FAUSTUS_USER`/`FAUSTUS_PASSWORD`, or `--user`/`--password`) — it logs in
the same way the Studio SPA does (`POST /api/auth/login`, cookie session)
and sends each turn the same way `studio/src/adapters/chat.ts`'s `sendTurn`
does (`POST /api/chat_stream`, `mode=agent`).

## Exit code and report

Exits non-zero when any time-sensitive question was not searched or was
refused, or any control question DID trigger a search. A dated report is
written to `docs/evals/freshness-<date>.md` with a per-question table
(question, control?, searched?, refused?, sources, seconds, verdict) and a
pass/fail summary — commit that file when a run is meant to be a recorded
result, the same way any other eval report in this directory is kept.

## What's covered without a live server

`tests/test_eval_freshness.py` exercises the SSE parsing (`iter_sse_events`)
and the pass/fail rules (`verdict_for`, `evaluate_stream`) against canned
event lists — no network, no running server required. `python3
scripts/eval_freshness.py --help` is expected to work standalone as a smoke
test that the script's own imports and argument parsing are sound.

`tests/test_freshness.py` covers the heuristic itself
(`src/freshness.py`'s `looks_time_sensitive`/`freshness_reasons`) with 30+
positive/negative cases in both languages — that is the actual detector this
battery is checking end to end; the battery is the live, slower check that
the detector's signal really does make the model call `web_search`.
