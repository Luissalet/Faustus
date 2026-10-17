---
name: local-voice-comparison
description: Compare local speech engines on pronunciation, continuity and latency using saved audio. Use when selecting a voice model, evaluating Spanish speech or testing a voice revision.
version: 1.0.0
category: testing
tags: [voice, audio, speech, spanish, voz, locucion, pronunciacion]
status: published
source: imported
---

## When to Use

Selecting or revising a local speech engine. Compare actual output from
installed weights and the intended hardware; vendor claims and model size
alone do not establish intelligibility, naturalness or useful latency.

## Procedure

1. Inventory installed engines, weight revisions, licences and supported
   languages. Use the local GPU scheduler to choose available hardware.
   Preserve the global reasoning service while evaluating media models.
2. Prepare a shared text set covering normal narration, names, numbers,
   punctuation, long passages and the languages actually needed. Keep the
   text, voice reference and generation settings with every output.
3. Generate each sample through the product's local engine. Measure first
   playable audio separately from total generation time. Include model
   loading time explicitly and distinguish a cold run from a warm one.
4. Listen to saved audio in a blinded or consistently ordered comparison.
   Check missing or repeated words, pronunciation, phrase boundaries,
   changes of speaker and continuity across streaming chunks. A transcript
   alone cannot establish voice similarity or naturalness.
5. Compare latency, peak memory and listening results against the actual
   use case. Select a default with a stated reason and retain the other
   engine when it serves a different need. Reproduce failures using the
   saved text, settings, model revision and audio file.
6. Start with [the Spanish scenario](references/scenario.json), expanding
   it with the user's real vocabulary and longer narration.

## Pitfalls

- A multilingual family name does not guarantee a specific checkpoint's languages.
- Fast first audio can conceal gaps or repetitions in later chunks.
- Normalisation and sample-rate conversion can affect perceived quality.

## Verification

- The installed checkpoint produces intelligible Spanish when required.
- Saved samples include actual settings and cold or warm measurements.
- A long sample retains its words and voice across chunk boundaries.
- Playback and export use the same generated audio without hidden regeneration.

## Provenance

Method reference: https://github.com/OpenBMB/VoxCPM at
cce58a5b59303c9bd63d12f23afe6a49a4a80c59 (Apache-2.0).
Original local comparison procedure; no hosted speech API required.
