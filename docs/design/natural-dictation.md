# Natural dictation: research and first audio experiments

2026-09-28. Research/prototype only; the production microphone flow is unchanged.

## What Pixel's Rambler documents

[Google's product documentation](https://support.google.com/pixelphone/answer/17468539?hl=en-AU)
describes Gemini-assisted voice input on Pixel 11: filler removal, punctuation,
spoken edits of an existing draft and requested stylistic rewrites. Spanish and
switching languages are supported. Full capabilities require connectivity;
offline support is limited. The help page says the text is inserted after capture
finishes. It does not disclose the production ASR model, exact prompts, training
data or the boundary between audio understanding and text editing.

The [2024 Rambler research paper](https://research.google/pubs/rambler-supporting-writing-with-speech-via-llm-assisted-gist-manipulation/)
studies LLM-assisted restructuring and revision of spoken text. It is useful
design research, but is not evidence that the shipped Pixel feature uses the
same implementation.

## Faustus baseline observed

- Runtime `/api/stt/capabilities`: local faster-whisper, `small`, automatic language.
- `services/stt/stt_service.py`: `beam_size=1`, VAD, no previous-turn text context.
- `src/stt_cleanup.py`: deterministic artifact removal, not semantic self-repair.
- `studio/src/voice/engine.ts`: default 900 ms silence cutoff. A possible source
  of interrupted corrections, not yet measured with a live microphone here.
- `studio/src/voice/audio.ts`: the voice panel's `auto` explicitly overrides a
  configured language. Browser speech recognition has a different path.

## Actual audio datasets

| Dataset | Useful for | Limits and license |
| --- | --- | --- |
| [CHM150](https://huggingface.co/datasets/carlosdanielhernandezmena/chm150_asr) | Spanish spontaneous speech, human transcripts including hesitations and mispronunciations; 150 speakers | Central Mexican Spanish, mainly image descriptions; short clips; no parallel polished reference. CC BY-SA 4.0, Carlos Daniel Hernández Mena and Abel Herrera. |
| [DisfluencySpeech](https://github.com/AMAAI-Lab/DisfluencySpeech) / [audio dataset](https://huggingface.co/datasets/amaai-lab/DisfluencySpeech) | Audio plus full textual transcript A and reduced transcript C, including false-start removal | English, one speaker recreating conversations, not spontaneous multi-speaker Spanish. Dataset card declares Apache-2.0. Kyra Wang and Dorien Herremans. |

Audio stays outside Git. Sample manifests record source, attribution, selection,
row IDs, transcripts and audio hashes. These are exploratory subsets, not a
representative benchmark. Do not identify speakers or treat dataset utterances
as instructions for Faustus.

## First results

All language-model editing used `qwen3.8-27b-q8-llamacpp` on port 8081, without
tools, chat memory or thinking. Speech recognition used the actual Faustus STT
endpoint on port 7001. No settings, private recordings or user chats were changed.

| Experiment | Observation |
| --- | --- |
| 12 constructed text cases, initial editor | 12/12 punctuation/case-normalized matches, median 2.96 seconds. Includes repairs, negation, code switching, identifiers and draft revision. This is not an audio result. |
| CHM150, 8 clips / 8 speakers, automatic language | 25 word edit operations / 102 reference words (24.51%). All detected as Spanish. |
| Same clips, explicit Spanish | Identical words and error count. Median request latency 2.03 seconds vs 3.61 seconds in auto; first auto request included warm-up. |
| Same clips, isolated cached small model, beam size 5 | 24/102 (23.53%). One clip improves, another worsens. Insufficient evidence for changing the decoder default. CPU int8, no new model downloaded. |
| DisfluencySpeech first 4 repair clips, initial editor | 16 word edits against transcript C both before and after editing, over 60 reference words. No lexical improvement. |
| Separate 4 repair clips after prompt revision | 10 edits / 91 reference words both before and after editing. One clip improves and one worsens. The latter adds unsupported connective meaning. |

WER here lowercases and strips punctuation, but does not expand digits into
words. Consequently a correct digit rendering can count as an error. Removing
a filler also counts against CHM150's verbatim-like reference. Its 24.51% figure
is **not** a measure of misunderstanding or final dictation usefulness. Likewise,
matching transcript C's editing conventions does not prove semantic fidelity.
Human review of names, negations, numbers and added meaning remains necessary.

The real clips exposed what constructed cases missed: ASR can substitute names
and content words, and a free-form editor can preserve errors or invent a smooth
connection. The initial unrestricted prototype is superseded by the constrained
editor below; neither can reliably recover words misheard by ASR.

## Integrated experimental editor

Faustus now separates two operations at `/api/stt/polish`:

- `clean`: remove fillers and abandoned alternatives, adjust punctuation; retain
  the original. Added or reordered words and changes inside literal quotes fall
  back to the original. This check limits invention, but does not prove semantic
  equivalence: deleting a word can still change meaning.
- `revise`: apply an explicit spoken instruction to the current draft. The voice
  panel keeps the draft visible and returns to review, without sending the edit.

The voice panel exposes natural dictation and original-transcript recovery.
An undo action restores the draft before the last voice correction, including
manual edits. Filler-only cleanup produces no message.
Composer dictation also uses cleanup. The editor uses the configured default chat
model, not the utility helper. Tests use qwen3.8-27b-q8-llamacpp on port 8081.
Timeouts preserve text, and cancellation invalidates late replies. Desktop-wide
dictation and meeting transcription are not yet connected to this editor.

Latest measured results (small exploratory samples, 2026-09-28):

- Production editor: 12/12 constructed cases meet normalized expectations. One
  quote case succeeds by rejecting an incorrect edit and keeping the original.
- Actual application endpoint, eight existing English ASR transcripts: 26 to 17
  word edits against 151 clean-reference words; development subset 16 to 8,
  held-out subset 10 to 9. The held-out set has one worse result, and recognition
  errors such as names remain. This is text editing after real-audio ASR, not a
  fresh end-to-end microphone test.
- Isolated large-v3-turbo CPU int8, same eight Spanish clips: 21/102 word edits,
  median about 7.66 seconds versus small's 25/102 and about 2.03 seconds with
  explicit Spanish. Filler removal and digit formatting affect this metric.
  No default recognizer change: the latency cost needs further evaluation.
- 36 backend checks, frontend capture/revision checks, TypeScript/build and a
  visual panel smoke check passed. Live microphone capture remains untested.
- A subsequent full audio-to-editor run through port 7001 reproduced the held-out
  10-to-9 result. Repeated editor inputs hit the existing response cache;
  that run must not be used to claim uncached end-to-end latency.

## Open-source precedents and design decisions

### Quantity fidelity regression (2026-10-05)

The deletion check accepted `-12` → `12`, `15%` → `15`, and `3/4` → `3`
because its tokenizer discarded meaningful symbols as punctuation. Signed
quantities, percentages, fractions and colon-separated times are now atomic
tokens. Spacing and the Unicode minus glyph can change without altering their
meaning; an explicit abandoned quantity may still be deleted in a correction.
The text evaluator now preserves these symbols in its scoring too.

Validation: 45 focused editor/cleanup tests pass and all 21 text scenarios match
through the production editor with qwen3.8-27b-q8-llamacpp on port 8081 (short
answers take the existing no-model path). These are text-stage checks, not new
audio accuracy measurements. This does not prove all semantic deletions safe.
Report: `D:/LocalAI/qa-rambler-numbers-isolated-20261005.json`.

### Follow-up: short turns and independent ASR comparison

The voice frontend previously rejected all normalized utterances of two or fewer
characters, including Spanish `sí` and `no`. Backend cleanup also discarded
ordinary acknowledgements (`gracias`, `thank you`, `thanks`, `you`). These rules
now preserve speech; known stock video-credit artifacts still have separate
filters. A subtitle request is no longer discarded just because it starts with
`subtítulos`. Text alone cannot prove that an acknowledgement is hallucinated.

The endpoint detector now accepts a brief answer (100 ms of positive level
samples); previously a response under 250 ms could leave it waiting up to its
60-second limit. An isolated positive sample expires at the 15-second empty
turn timeout. This is a regression fix to the existing energy detector, not a
claim of semantic endpointing or measured microphone accuracy.

Complete short answers bypass the LLM editor. Actual development HTTP checks
for `sí`, `no`, `gracias`, and `42` took 7–51 ms instead of the prior 2.4–3.8 s
editing calls. Audio recognition latency is additional. An 18-case text suite
with 3.8 also passes, including successive corrections. Backend checks: 57;
component tests verify short answers reach review and send exactly once.

Following Handy's model choices, an isolated `onnx-asr` 0.12.0 / ONNX Runtime
1.30.0 CPU experiment evaluated Parakeet V3 INT8. Its
[runtime](https://github.com/istupakov/onnx-asr) is MIT; the
[NVIDIA model / ONNX conversion](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx)
is CC BY 4.0. No Faustus dependencies or recognizer settings were changed.

| Spanish sample | Whisper small word edits | Parakeet word edits | Median recognition time: small / Parakeet |
| --- | --- | --- | --- |
| Original 8 clips, 102 reference words | 25 | 9 | 2.03 / 0.605 seconds |
| Additional 8 clips, 101 reference words | 14 | 8 | 1.796 / 0.556 seconds |

The second sample uses windows at 200, 900, 1600 and 2300; its eight speakers
do not overlap the original sample. Results are exploratory central-Mexican
image descriptions, not broad Spanish validation. One held-out clip worsens
from 4 to 6 edits with Parakeet. WER includes fillers, accents and number
formatting. Parakeet output is raw; the Whisper baseline uses Faustus cleanup.
Parakeet's four English held-out clips have 3 edits / 109 verbatim-reference
words, which is a different target from the polished-reference editor metric.
At this evaluation stage it was not yet a Faustus provider (integration below). Its startup
and download time are reported separately; it is not a streaming recognizer.

Reproduce with `scripts/eval_dictation_onnx.py --directory <manifest-directory>
--output <report.json>` in a separate environment containing `onnx-asr[cpu,hub]`,
`soundfile` and `requests`. The command explicitly downloads the selected weights
on first use. Local reports: `parakeet-chm150.json`,
`parakeet-chm150-heldout.json`, `small-chm150-heldout.json`, `parakeet-heldout.json`
under the QA directory above. Empty dataset selections now fail visibly.

### Optional Parakeet integration

`stt_provider=parakeet` now selects an optional native local provider, independent
of Whisper's model/device settings. Install `requirements-voice-parakeet.txt`;
discovery checks dependencies without loading or downloading weights. First
transcription loads CPU INT8 weights and Silero VAD once. A lock serializes the
shared model. Audio formats are decoded through PyAV into 16 kHz mono PCM.
VAD uses a 100 ms minimum speech duration, a 20-second maximum segment, and
200 ms padding. Segment timestamps are coarse speech boundaries, not alignment.
No language ID is fabricated: responses report automatic language mode and an
empty language field. It never silently substitutes Whisper on failure.

Actual integrated checks on this machine:

- Eight Spanish clips decoded with the new VAD adapter; warm requests roughly
  0.63–0.81 seconds. First load plus initial VAD download took 5.68 seconds.
- Exact digital silence and seeded quiet noise produced zero segments.
- A constructed 55.51-second fixture alternating two real utterances and
  one-second silences produced eight correctly ordered segments in 2.66 seconds.
  This is a composite fixture, not spontaneous continuous speech.
- Actual development HTTP route → editor passed two human clips and four of
  five synthetic Spanish probes from Windows Helena. `No`, `Gracias`, `Cuarenta
  y dos`, and a Tuesday→Thursday correction survived; isolated `Sí` became
  English `See.`. The editor correctly did not invent a Spanish correction.
  Automatic language ambiguity is a known limitation; this is not ready to
  replace explicit-language Whisper for every user.
- 61 focused backend checks passed; UI typecheck/build and visual settings
  smoke passed. No user microphone was recorded. Production preference remains
  unchanged; the temporary development-provider switch was restored to `local`.

Reports: `parakeet-adapter.json`, `parakeet-live-pipeline.json`,
`parakeet-synthetic-pipeline.json` in the local QA directory. The isolated raw
benchmark used ONNX Runtime 1.30.0; this integrated adapter used the existing
Faustus 1.29.0 runtime and onnx-asr 0.12.0, installed without upgrading existing
dependencies. Synthetic recordings stay outside Git.

These are inspected references, not dependencies or claims of code reuse:

| Reference | Concept worth adapting | Faustus decision |
| --- | --- | --- |
| [Academic Rambler](https://github.com/BerkeleyHCI/rambler), [paper](https://arxiv.org/abs/2401.10838) | Speech as editable idea blocks, semantic zoom, merging/splitting and requested edits | Keep original speech and explicit editable drafts. Consider idea-block operations after basic dictation quality is established. This is not Pixel's implementation; repository has no root license listed, so do not copy its code without clarifying permission. |
| [OpenTypeless pipeline](https://github.com/tover0314-w/opentypeless/blob/main/src-tauri/src/pipeline.rs) (MIT) | Distinct recording/transcription/polishing/output states, selected-text edits, raw and polished text | Separate cleanup from revision, preserve drafts, and measure each stage. Its desktop capture/clipboard infrastructure is not needed inside Faustus. |
| [OpenWhispr routing](https://github.com/OpenWhispr/openwhispr/blob/main/src/helpers/dictationRouting.js) (MIT) | Explicit cleanup, translation and agent routes; an assistant request does not silently become cleanup | Spoken draft correction has its own route and failure behavior. It must not be submitted as an ordinary message on failure. |
| [Handy](https://github.com/cjpais/Handy) (MIT) | Hold/toggle capture, Silero VAD, interchangeable Whisper/Parakeet engines | Preserve manual completion; benchmark end-of-turn handling and CPU-friendly ASR before choosing a larger default. VAD filtering is not proof of good conversational endpointing. |
| [Ramblr](https://github.com/trevornk/ramblr), [cleanup design](https://github.com/trevornk/ramblr/blob/main/docs/adr/0001-cleanup-waterfall.md) (GPL-3.0) | Optional cleanup with raw fallback, bounded foreground latency, segmented transcription | Keep the successful raw result if polishing fails. Investigate segment accumulation to reduce final waiting; avoid adding a multi-provider chain without need. |

Next quality gates: broader Spanish accents and code switching, names and spoken
numbers, pauses inside corrections, repeated draft edits and undo, and long speech.
Measure content preservation separately from WER and end-to-end latency. Keep this
integrated in Faustus until a genuinely reusable standalone component emerges.

## Spoken formatting (2026-10-05)

The natural editor now interprets spoken `nueva línea` / `new line`,
`punto y aparte` / `new paragraph`, and `punto final` / `full stop` as
formatting when used as commands. Quoted phrases and discussion of the words
remain literal. This is part of the existing cleanup call, with no extra model
request or blind phrase replacement.

An isolated text-stage comparison against `qwen3.8-27b-q8-llamacpp` on port 8081
improved four formatting cases from 1/4 to 4/4, taking 2.30–4.56 seconds per
case after the change. The evaluator now checks line and paragraph boundaries:
word-only normalization previously hid missing paragraph breaks. These are
synthetic transcript tests, not evidence of recognizing those commands from audio.
Reports: `D:/LocalAI/qa-rambler-format-baseline-20261005.json` and
`D:/LocalAI/qa-rambler-format-after-20261005.json`. Re-score the baseline with
the current `matches_expected` function; its original word-only scores missed
two formatting failures.

A subsequent 25-case production-editor run matched 18 expected outputs but
encountered 11 model timeouts, preserving the original transcript or draft each
time. All completed model responses matched their expected text. This does not
pass the end-to-end reliability gate: the formatting feature worked in the
focused run, while model latency remains an unresolved limit. The model health
endpoint still returned `ok`; the cause of the slow responses was not established.
See `D:/LocalAI/qa-rambler-format-full-20261005.json`. The focused backend suite
(natural dictation, STT cleanup and Parakeet) passed 50 tests.

Three additional cases exposed remaining limits: quoted formatting phrases were
preserved, a combined correction plus line break timed out, and an English list
was rejected as a non-faithful edit. An instrumented repeat showed the model
returning `Ingredients\nmilk\ne`, truncating `eggs` to `e`; Faustus correctly
kept the original rather than accepting it. Do not treat English list formatting
or combined correction/formatting as reliably validated yet. Follow-up report:
`D:/LocalAI/qa-rambler-format-heldout-20261005.json`.

### Follow-up: explicit lines (2026-10-05)

The editor can now return `{"text": ["first line", "", "next paragraph"]}`;
Faustus joins validated strings with newlines and keeps the public API's `text`
field a string. Single-string model responses remain supported. This avoids
requiring the model to combine JSON newline escapes with the following word.
The previously truncated English list now retains `eggs` in full. Blank entries
preserve paragraph boundaries, and the same fidelity checks run after joining.

The updated production editor matched all 28 expected outputs on Qwen 3.8,
with no timeouts in this run. One quoted-hesitation proposal was rejected and
fell back to the already correct original; the other 27 cases passed without
fallback (four short responses bypass the model). Model-completed calls had
median latency 2.56 seconds. Three additional unseen cases also passed: signed
quantities/percentages/fractions in a list, another English list, and a Spanish
paragraph. Reports: `D:/LocalAI/qa-rambler-lines-full-20261005.json` and
`D:/LocalAI/qa-rambler-lines-heldout-20261005.json`. The backend suite passed
56 tests, including malformed arrays and paragraph preservation. This remains
text-stage evidence; it does not establish microphone recognition quality or
explain the previous run's server timeouts.

Research checked [llama.cpp's request sampling options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
and [vLLM's transcription defaults](https://github.com/vllm-project/vllm/blob/main/vllm/entrypoints/speech_to_text/transcription/protocol.py).
An isolated attempt with repetition penalties disabled still produced the same
truncated word, so sampling defaults were not changed. Raw server output confirmed
the truncation came from generation with `finish_reason: stop`, not JSON parsing.

### Revision-chain failure investigation (2026-10-05, later run)

`scripts/eval_dictation_revisions.py` now tests six consecutive edits, feeding
each actual result into the next turn: change a day, contradict that change,
insert a paragraph, change the time, remove the last sentence, and append a
paragraph. The expected draft is never substituted for a failed actual result.
All six turns failed in the initial live run: the first timed out, then later
responses contained malformed JSON such as `{"textVC`. The draft stayed intact.
The server health endpoint still reported `ok`.

A diagnostic repeat with per-request `cache_prompt: false` also failed all six
turns (four timeouts and two malformed responses). This option is documented in
the [llama.cpp server API](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
No server settings or sampling defaults were changed; disabling prompt reuse
did not establish or fix the root cause. Reports:
`D:/LocalAI/qa-rambler-revision-chain-20261005.json` (original run) and
`D:/LocalAI/qa-rambler-revision-chain-diagnostic-20261005.json` (no-cache run).

The editor now reports malformed JSON, missing text and invalid line arrays as
`invalid_model_output`, separately from transport failures (`editor_unavailable`).
Both preserve the draft; raw private text is not added to diagnostics. The
focused backend suite passes 60 tests. Earlier successful runs should not be
read as evidence that the current live server is reliable.

### Confirmed engine failure, not editor parsing (2026-10-05)

A direct HTTP request to Qwen 3.8 on 8081 for `7+5`, reasoning disabled,
returned `kowaVCVC...` and reached the 20-token limit. Repeating with
`cache_prompt: false` gave the same output with `cache_n: 0`. The rendered
chat template contains real newlines and correct ChatML boundaries. Runtime:
`b11040-5b335f413`, Q8_0, 131072 context, one slot, four GPUs, speculative
decoding disabled. HTTP `/health` still says `ok`.

The recovery probe now checks generated arithmetic answers instead of accepting
anything that lacks a whole-response repetition pattern. It confirms an
incorrect answer with a second trivial problem; a transport error or empty
response remains inconclusive. The updated probe classified this live engine
as broken. Combined recovery, engine and dictation tests: 99 passed, one
POSIX-only integration test skipped on Windows.

Clearing a slot through `/slots/0?action=erase` was unavailable (HTTP 501:
requires `--slot-save-path`). A process restart was denied by Windows because
the engine belongs to `luism`, while this session runs as `codexsandboxoffline`.
`D:/LocalAI/Restart-RamblerEngine.ps1` is prepared for the owning account: it
validates the model/executable and idle slot, reloads the current configuration,
and checks an actual answer after loading. The owning account subsequently ran
it successfully; post-reload measurements are recorded below. The underlying
runtime defect is not claimed fixed.

Primary research reports similar [Qwen CUDA DeltaNet corruption](https://github.com/ggml-org/llama.cpp/discussions/27164)
and [recurrent-state leakage on HIP](https://github.com/ggml-org/llama.cpp/issues/29092).
Neither establishes the cause here: this CUDA build is newer than the older
DeltaNet fix, and HIP is a different backend. The next discriminator is a
matched runtime/backend comparison if corruption returns after the verified
fresh load. All local executable/DLL timestamps match the same release date;
that alone does not prove which DLLs were loaded.

### Verified reload and long-dictation deadline (2026-10-05)

The user reloaded the same model and runtime configuration at 18:32; PID 212024
answered `7+5` with `12`. The production editor then passed the six-turn revision
chain and all 31 existing text cases. One quoted-hesitation proposal fell back to
the already correct original; the other cases needed no fallback. Reports:
`D:/LocalAI/qa-rambler-revision-reloaded-20261005.json` and
`D:/LocalAI/qa-rambler-full-reloaded-20261005.json`. This confirms recovery after
reload, without establishing which runtime component originally corrupted state.

A separate long Spanish dictation still exhausted the fixed 12-second request
deadline at 12.25 seconds, while a generated arithmetic probe confirmed the engine
remained sane (`D:/LocalAI/qa-rambler-long-reloaded-20261005.json`). The editor now
budgets 12–90 seconds according to the combined transcript and draft length.
Both natural cleanup and spoken revision in Studio use a matching deadline with
network headroom; short corrections to long drafts count the draft too.

Two added production-editor cases passed with the new budget: complete long
dictation in 15.78 seconds and a correction preserving that draft in 13.99 seconds.
Report: `D:/LocalAI/qa-rambler-long-adaptive-20261005.json`. The text suite now
contains 33 cases. The previous 31-case run and this focused two-case run are
separate measurements, not a single full run after the deadline change.

The recovery detector also recognizes repeated output with a short corrupt prefix.
Malformed structured dictation output schedules a background generation-health
check, while returning the original draft. An inconclusive health check does not
trigger a restart; engine reload still requires an accessible managed process or
configured launcher. These changes detect and recover from symptoms; they do not
repair a CUDA kernel or guarantee corruption cannot recur.

Verification: 105 backend tests passed, one POSIX-only integration test skipped on
Windows; four frontend voice checks, TypeScript checking and the production
frontend build passed. The frontend
checks exercise actual capture and revision paths with mocked recognition/fetch,
including the long-text deadlines. These are text-editor and regression results,
not a microphone-recognition or running-app end-to-end evaluation.

### Automatic recovery of external runners (2026-10-05)

The remaining manual-recovery gap was an unsupervised runner started outside
Faustus. Recovery now snapshots an identifiable local llama-server's executable,
argv, working directory and environment and relaunches that exact configuration.
This path precedes a managed profile which might describe a different model on
the same port. A shell parent counts as a supervisor only when its script or
inline command contains a continuous relaunch loop; an interactive terminal or
one-shot start script cannot keep a killed server running.

Faustus's startup/shutdown owns a generation-health monitor. Every 120 seconds it
reads `/slots` for the running default local endpoint and sends arithmetic probes
only when every slot is idle and reports `n_prompt_tokens: 0`. An idle slot retaining
chat tokens is skipped so the probe cannot evict a reusable prefix; missing or
malformed prompt-token state is also skipped. Malformed output and repeated garbage
retain their symptom-triggered background checks.
Two incorrect arithmetic probes confirm corruption; unavailable generation is
inconclusive. Busy slots defer recovery, and slots are checked again after the
probe before stopping a runner. The PID identity is checked before termination.
Existing per-port coordination and restart cooldown remain in effect. Shutdown
cancels shared recovery attempts so they cannot relaunch a model after Stop All.

An isolated Windows process integration test produced corrupt responses, then
exercised the real HTTP probes, process termination, replacement spawn and
post-reload verification. The replacement used a different PID and preserved an
injected environment value. The fixture was a tiny HTTP server, not a model;
the live Qwen 3.8 was separately checked as healthy and left running. This does not exercise a real CUDA reload. In the recorded Windows check, normal
`PROCESS_TERMINATE` succeeded and the Qwen listener returned as PID 61412. That
result applies to this observed process and account; it does not establish a
general permission rule for other processes or Windows accounts.

The combined engine, recovery, dictation and STT suite passed 124 tests, with one
POSIX integration skipped on Windows. The live Qwen 3.8 then passed all six
chained spoken-revision text cases again, without fallback:
`D:/LocalAI/qa-rambler-revision-autoheal-20261005.json`.

The monitor becomes active when the updated Faustus application starts. The
already-running Qwen server does not acquire a watchdog merely from editing this
module.

## Reproduce

Use a directory outside the repository. The scripts download selected public
clips only; neither downloads the whole corpus nor changes Faustus settings.

```powershell
python scripts/eval_natural_dictation.py --output D:/LocalAI/qa-natural-dictation/text.json
python scripts/eval_natural_dictation.py --production-editor --output D:/LocalAI/qa-natural-dictation/production-text.json
python scripts/eval_dictation_revisions.py --output D:/LocalAI/qa-natural-dictation/revision-chain.json
python scripts/eval_dictation_audio.py --prepare --directory D:/LocalAI/qa-natural-dictation/chm150
python scripts/eval_dictation_audio.py --prepare --corpus disfluency --language en --polish --directory D:/LocalAI/qa-natural-dictation/repairs
python scripts/eval_dictation_audio.py --prepare --corpus disfluency --sample-offset 100 --language en --polish --directory D:/LocalAI/qa-natural-dictation/heldout
./venv/Scripts/python.exe scripts/eval_dictation_audio.py --local-model small --beam-size 5 --language es --directory D:/LocalAI/qa-natural-dictation/chm150
./venv/Scripts/python.exe scripts/eval_dictation_audio.py --production-editor --language en --directory D:/LocalAI/qa-natural-dictation/heldout --output D:/LocalAI/qa-natural-dictation/production-audio.json
```

The initial results are under `D:/LocalAI/qa-natural-dictation-20260928` on the
development machine. The current editor prompt includes the second revision;
the saved initial results intentionally remain separate.

## Implementation direction

1. Improve recognition first: compare a stronger multilingual ASR on these same
   clips, then broader Spanish accents, noise, names and code switching. Evaluate
   context vocabulary as a bias, not a source of replacement facts.
2. Separate faithful cleanup from requested rewriting. Cleanup should primarily
   remove disfluencies and explicit abandoned alternatives, preserve negations
   and numbers, and retain the raw transcript. Treat ASR punctuation as provisional.
3. Give spoken revisions an explicit current draft. Editing a draft must not
   execute the actions mentioned inside it.
4. Measure turn endings using actual audio with pauses and corrections. Compare
   manual stop with automatic endpointing before changing silence thresholds.
5. Integrate only after real audio improvements and acceptable end-to-end latency
   are demonstrated. Keep a way to recover the original transcription.
