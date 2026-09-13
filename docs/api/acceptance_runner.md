# Acceptance runner (acceptance parity)

`scripts/acceptance_run.py` is PR1's acceptance executor (see
`docs/spec/paridad/README.md`): it runs every test marked
`@pytest.mark.acceptance("A0N")`
under `tests/` and writes one machine-readable JSONL line **per case** from
`docs/spec/paridad/acceptance_cases.json` — all 36 by default, including
the ones with no test yet, which get `outcome: "NOT_EXECUTED"` instead of
being silently missing from the file. It never decides a case is closed —
that's `docs/spec/paridad/ESTADO_ACEPTACION.md`'s job, kept honest by
`tests/test_acceptance_index.py`. This script only reports what actually ran
and what its actual outcome was.

## Running it

```bash
python3 scripts/acceptance_run.py
# writes data/acceptance/<run_id>.jsonl (36 lines) and prints a Markdown
# summary table to stdout.

python3 scripts/acceptance_run.py --case A07
# only A07's line is produced (still NOT_EXECUTED if A07 has no test yet).
```

Other flags: `--target DIR` (default `tests`) restricts where pytest looks
for acceptance-marked tests — used by `tests/test_acceptance_run.py` to
point the runner at a throwaway test tree instead of the real suite;
`--cases-path FILE` (default `docs/spec/paridad/acceptance_cases.json`)
overrides the case-id list, for the same reason; `--out FILE` overrides the
JSONL destination; `--system NAME` overrides the `system` field (default
`faustus`, so the same JSONL shape can later hold a comparison run against
another system).

## JSONL line shape

One JSON object per line, matching `acceptance_cases.json`'s
`required_run_fields` exactly:

| Field | Meaning |
| --- | --- |
| `case_id` | `A01`..`A36` |
| `system` | `"faustus"` by default (`--system` overrides) |
| `commit` | `git rev-parse HEAD` at run time, `"unknown"` if git is unavailable |
| `config_hash` | sha256 of `src.settings.load_settings()` (effective settings merged with defaults), or `"none"` if settings could not be loaded at all — never a hash of nothing pretending to be real |
| `model` | `"scripted"` unless a test's evidence declares another (see below) |
| `run_id` | one UUID per invocation of the script, shared by every line it writes (and by the output filename `data/acceptance/<run_id>.jsonl`) |
| `attempts` | `1` for a case with at least one collected test (no rerun-on-failure plugin is configured in this repo, so this is never higher today), `0` for `NOT_EXECUTED` |
| `outcome` | `passed` \| `failed` \| `xfailed` \| `skipped` \| `NOT_EXECUTED` — see aggregation below |
| `evidence_refs` | list of `{"nodeid": "...", ...}`, one entry per test function behind the case, `...` being whatever `tests/acceptance/conftest.py::record_evidence()` attached |
| `cost_status` | `"unpriced"` (default — scripted/fake model, no real spend), `"none"` (`NOT_EXECUTED`, nothing ran), or `"unknown"` if a test's evidence says so |
| `total_cost` | `0`, or the literal string `"unknown"` when `cost_status` is `"unknown"` — **never `0` when the real cost is unknown** |
| `latency_ms` | sum of the `call`-phase durations of every test behind the case (`0` for `NOT_EXECUTED`) |

## Aggregating several tests into one case

A case can have more than one test function behind it (e.g. A01's three
sub-scenarios). The case-level `outcome` is the **worst** of its tests'
outcomes, in this order: `failed` > `xfailed` > `skipped` > `passed` — one
failing sub-scenario means the case is not green, even if the other two
pass. `latency_ms` sums across all of them; `evidence_refs` lists all of
them.

## How a test declares evidence

```python
from tests.acceptance.conftest import record_evidence

@pytest.mark.acceptance("A07")
def test_cancel_cascade_stops_every_descendant(client, request):
    ...
    record_evidence(request, parent_session_id=parent_sid, workers_stopped=stopped)
```

`record_evidence` may also set `model`, `cost_status`, or `total_cost` to
override this runner's defaults for that specific test — used for a case
whose test runs against a real priced endpoint instead of the scripted/fake
model every other acceptance test uses today.

## What this script does NOT do

It does not decide whether a case is closed (that's the `verde`/`xfail`/
`pendiente` state in `docs/spec/paridad/ESTADO_ACEPTACION.md`, checked
against the real markers by `tests/test_acceptance_index.py`), and it does
not compare against a baseline or another system — `TF01`/`TF24` (the
comparable-benchmark and full-ledger backlog items,
`docs/spec/paridad/backlog.csv`) are still open. It is the first, honest
half of "measure": a real, per-case, evidence-linked run record.
