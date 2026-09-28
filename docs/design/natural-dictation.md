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
connection. Do not enable this prototype as an automatic replacement for speech.

## Reproduce

Use a directory outside the repository. The scripts download selected public
clips only; neither downloads the whole corpus nor changes Faustus settings.

```powershell
python scripts/eval_natural_dictation.py --output D:/LocalAI/qa-natural-dictation/text.json
python scripts/eval_dictation_audio.py --prepare --directory D:/LocalAI/qa-natural-dictation/chm150
python scripts/eval_dictation_audio.py --prepare --corpus disfluency --language en --polish --directory D:/LocalAI/qa-natural-dictation/repairs
python scripts/eval_dictation_audio.py --prepare --corpus disfluency --sample-offset 100 --language en --polish --directory D:/LocalAI/qa-natural-dictation/heldout
./venv/Scripts/python.exe scripts/eval_dictation_audio.py --local-model small --beam-size 5 --language es --directory D:/LocalAI/qa-natural-dictation/chm150
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
