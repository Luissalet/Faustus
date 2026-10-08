# Constrained choice (OBJ-27, phase 1)

Closed-set decisions ("which of these N labels?") used to be answered with
free text, cut at a small token budget and then repaired with keyword scans.
`src/constrained_choice.py` asks the model for the choice through the
strongest constraint the backend supports and reports which path it took.

## What it does

`choose_one(options, prompt, ...)` returns a `ChoiceResult` (choice, index,
path, constrained, honoured, repaired, backend, ms, prompt/completion tokens,
raw reply, reason). It never raises for runtime failures; the choice is `None`
when nothing valid came back.

| backend (positively identified) | what is sent | path |
|---|---|---|
| llama.cpp server | `grammar` (GBNF `root ::= "a" \| "b"`), thinking off, helper slot | `llamacpp_grammar` |
| native Ollama | `format` = `{"type":"string","enum":[...]}` | `ollama_format` |
| anything else (hosted providers, unknown servers) | plain prompt with the options appended | `free_text` |

Unknown fields are never sent to backends that were not positively identified,
because strict hosted providers reject them.

Fallbacks, in order:

1. HTTP 400/422/501 on a grammar is remembered for 600 s for that endpoint;
   the request is retried as free text. Any HTTP error on a constrained
   attempt falls back to free text. Timeouts and connection errors do not
   retry.
2. Free-text replies go through a validated parse: exact match, NFKC
   case-folded unique match, trimmed punctuation, unique whole-word match
   (longest label first), JSON unwrapping, think-block stripping. Two different
   options in one reply are ambiguous and count as no match. That includes a
   JSON object whose answer fields disagree (`{"choice":"a","answer":"b"}`,
   keys compared case-insensitively, a one-element list counts as a value) and
   a repeated key with different values; the reply goes to the repair, never
   to the first field. Fields that agree (also once folded) are accepted, and
   prose fields such as a reason are not answers.
3. One repair call restates the allowed answers. After that the result is
   `None` with the reason recorded.

Settings (registered in `DEFAULT_SETTINGS`, shown on the Agent settings screen with
a Spanish label and help): `constrained_choice_enabled` (default true) and
`constrained_choice_backend` (`auto`; or force `llamacpp`, `ollama`, `free`).
The defaults keep the behaviour of this note; a test pins that the module
defaults and `DEFAULT_SETTINGS` agree.

## Migrated decision point

`DeepResearcher._classify_category` (report category: product, comparison,
howto, factcheck, or general) now calls `choose_one` with the category labels
plus `general`. Previously it asked for one word with `max_tokens=20`, then
scanned the reply for category keywords; a verbose reply mentioning two
categories silently picked the first keyword. The decision is stored in
`DeepResearcher.category_decision`. With `constrained_choice_enabled` off the
previous implementation (`_classify_category_free_text`) runs unchanged.

## Measurement (stub, no model loaded)

All numbers below come from `scripts/constrained_choice_bench.py`, a stub
chat-completions, llama-server-shaped and Ollama-shaped server. They are
stub numbers, not real-model numbers.

Assumptions, stated explicitly:

- Latency is simulated as 5 ms + 2 ms per prompt token + 80 ms per completion
  token (about 12.5 tokens/s decode, inside the 8-15 tokens/s recorded for a
  local 27B model). The prefill cost is a guess.
- Tokens use the codebase heuristic (0.3 per character, 4 per message
  overhead); they are not tokenizer counts.
- The unconstrained replies are scripted: the cycle
  `clean, preamble, clean, verbose, clean, invented` stands for a model that
  is only sometimes disciplined. The second table uses `clean` only, i.e. a
  disciplined model.
- 24 fixed cases (Spanish and English; product, comparison, howto, factcheck,
  general). Request count includes repair calls.

Mixed-style model (24 cases):

| backend | path | requests | prompt tok | completion tok | ms total | final in set | final None |
|---|---|---:|---:|---:|---:|---:|---:|
| llamacpp | previous | 24 | 1988 | 138 | 15631.7 | 24/24 | 6 |
| llamacpp | constrained | 24 | 1881 | 48 | 8183.0 | 24/24 | 3 |
| ollama | previous | 24 | 1988 | 138 | 23905.8 | 24/24 | 6 |
| ollama | constrained | 24 | 1881 | 58 | 16552.5 | 24/24 | 3 |
| plain | previous | 24 | 1988 | 138 | 15595.2 | 24/24 | 6 |
| plain | constrained | 28 | 2415 | 165 | 18710.5 | 24/24 | 3 |

Saved (previous minus constrained): llamacpp 90 completion tokens (65.2%) and
7448.7 ms (47.7%); ollama 80 tokens (58.0%) and 7353.3 ms (30.8%); plain -27
tokens and -3115.3 ms (the repair calls cost more).

Disciplined model (all replies clean):

| backend | completion tok previous / constrained | ms previous / constrained |
|---|---|---|
| llamacpp | 48 / 48 | 8481.7 / 8161.3 (3.8% saved) |
| ollama | 48 / 58 | 15454.5 / 16588.8 (-7.3%) |
| plain | 48 / 48 | 8434.0 / 8354.6 (0.9% saved) |

Findings:

- The final answer was inside the set 24/24 times on every backend and path.
  The difference is in the raw reply: the previous path's last raw reply was
  outside the set in 12 of 24 cases (llama.cpp and plain) and the old keyword
  scan then recovered or lost the category; the constrained path had 0 raw
  replies outside the set on llama.cpp and Ollama.
- "None" results fell from 6 to 3 of 24 (the 3 remaining are the genuine
  `general` cases).
- Against a disciplined model the saving is only the shorter prompt (about 5%)
  and no completion tokens. Constraining pays off when the model would
  otherwise ramble; it is not a speedup on its own.
- Request bytes grow about 12% (grammar or schema plus the options line).
- On a plain backend the validated parse plus one repair costs more calls than
  before (28 requests for 24 cases) but recovers categories the old scan lost.
- Ollama with a top-level string enum adds 10 completion tokens over 24 cases
  (the JSON quotes). The Ollama absolute latency of both paths is higher than
  llama.cpp in the stub because of the surrounding route (probes and slot
  handling), identical in both paths; the comparison is paired.

## Verification

- `tests/test_constrained_choice_conflicts.py` (27 tests): contradicting JSON
  replies, duplicate keys, the repair path, and the settings registration.
- `tests/test_constrained_choice.py` (69 tests): builders and escaping,
  matching, backend routing, HTTP behaviour against the stub (constraint on
  the wire, free text, repair, ignored grammar, rejected-and-remembered
  grammar, refused Ollama schema, master switch, timeout, no endpoint), 24
  cases never outside the set on three backends, stats.
- `tests/test_constrained_choice_category.py` (10 tests): parity of the
  migrated point on 24 cases, legality where the previous path lost
  categories, switch-off restores the previous call.
- Live: isolated Faustus on a spare port with the research endpoint pointed at
  the stub; `POST /api/research/start` drove the real route and the log showed
  `Auto-detected category: X (path=...)` for llama.cpp, plain and Ollama
  shapes.

## Limits

- Real-model numbers are pending: no model was loaded for this phase.
- The top-level enum shape for Ollama `format` is verified only against the
  stub; recent Ollama versions accept any JSON schema, but this has not been
  checked on a real server.
- The direct llama.cpp request bypasses the per-turn spend and trace layer;
  usage is read from the server response.
- Other closed-set decision points still use their previous code.
