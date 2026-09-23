# Typed decisions eval

Measures what a typed decision (`src/typed_decision.py`: one prefill, the
next-token probabilities of the allowed answer letters) adds on top of the
deterministic rules it sits behind, with the exact prompts the product
sends. See FAUSTUS.md §177 and [docs/api/typed_decision.md](../api/typed_decision.md).

## Cases

`docs/evals/typed_decision_cases.json` — invented, bilingual (ES/EN):

- **freshness** (60, half positive): "does answering this need information
  that changes over time?" — the question the chat route asks when the
  keyword rule of `src/freshness.py` is unsure. Includes the rule's known
  blind spots on both sides: questions with no keyword that do need the web
  ("who runs the council these days?") and bare time words that do not
  ("what does the last verse mean?", "the current in a 10 V circuit").
- **entity_types** (40, five per type): which of the brain's entity types
  a named thing is in its sentence — the field the brain's typing pass asks
  (`src/brain/extract.py::entity_type_field`).

## What is reported

Per task:

| measure | meaning |
|---|---|
| `rule` | accuracy of the deterministic rule alone (for entity types the rule always says `other`) |
| `typed` | accuracy of the raw decision (argmax) on every case |
| `combined` | accuracy of what ships: rule first; the decision only where the rule is unsure (freshness) or the entity is untyped (entity types), and only above `typed_decision_min_confidence` and `typed_decision_min_mass` |
| `flips (right)` | how many answers the combined behaviour changed w.r.t. the rule, and how many of those changes were right |
| `typed when accepted` | accuracy of the decisions that passed the thresholds |
| calibration | stated confidence vs observed accuracy, in buckets |
| latency | p50/p95 of the first field on a context |
| shared prefix | (freshness) p50 of a SECOND field asked on the same context right after — "is this written in Spanish?", whose accuracy is also printed as a sanity check — and the cached prompt tokens the server reports for each |

For freshness the report also breaks out the cases where the rule was
unsure (the only ones the product ever asks about).

## Running it

```bash
# a loopback OpenAI-compatible helper (llama-server)
python scripts/eval_typed_decision.py --url http://127.0.0.1:8082/v1 --model <name>
# a local Ollama (the native /api/chat is used automatically)
python scripts/eval_typed_decision.py --url http://127.0.0.1:11434 --model <name>
# whatever the Utility model setting resolves to on this install
python scripts/eval_typed_decision.py
# options
#   --markdown / --json      output format (the table below is --markdown)
#   --task freshness|entity_types, --limit N
#   --min-confidence X --min-mass Y   thresholds (default: the settings)
#   --timeout-ms N           per request (default 15000)
#   --allow-load             call a model that is not resident (default: refuse)
# offline, deterministic, for CI (tests/test_eval_typed_decision.py)
python scripts/eval_typed_decision.py --fake --markdown
```

The model is never loaded behind your back: if it is not resident at
`--url`, the script says so and exits with code 2 unless `--allow-load`.

## Results

### Fake model (CI only — says nothing about any real model)

The `--fake` server is a keyword matcher that answers in the OpenAI-shaped
log-probability format and mimics a prompt cache. Its numbers only prove
the pipeline end to end:

```
| task | n | rule | typed (raw) | combined | flips (right) | typed when accepted |
| freshness | 60 | 73% | 97% | 97% | 14 (14) | 97% |
| entity_types | 40 | 12% | 95% | 60% | 21 (19) | 90% |
```

### Live endpoints

Pending: to be run on the owner's machine against the loopback helper and
against Ollama (see PENDIENTES.md). Paste the `--markdown` output of each
run here with the date, the endpoint kind and the model size.

## Results — 23-09-2026, owner's machine

Loopback helper (a 3B model on llama-server, already resident; nothing was
loaded for the run). Thresholds: confidence ≥ 0.7, mass ≥ 0.5.

| task | n | rule | typed (raw) | combined | flips (right) | typed when accepted | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|
| freshness | 60 | 73% | 92% | 93% | 14 (13) | 93% | 389 | 501 |
| entity_types | 40 | 12% | 85% | 88% | 39 (34) | 87% | 395 | 544 |

Shared prefix: first field p50 389 ms, second field on the same context
384 ms; cached prompt tokens (mean) 40 on the first field, 53 on the second.
The prompt is short, so the saving is small here; it grows with the context.

Calibration (freshness): 57 of 60 decisions above 0.95 confidence, 95%
right there; the three below 0.95 were right once. Entity types: 36 of 40
above 0.95, 89% right. High confidence is not proof on a 3B model: the
thresholds stay where they are and the rule keeps the first word.

The run against the big model on Ollama was not repeated here on purpose:
it would have had to load or share a 27B that another instance was using.
