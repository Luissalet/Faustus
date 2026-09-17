# Voice mode

`studio/src/voice/` implements voice as a real conversation, not a
dictation form: by default the panel listens as soon as it opens, sends
what it heard when you stop talking, reads the answer aloud, and starts
listening again — no buttons required. See `docs/ui/voice-surface.md` for
the visual design of the panel and the reactive orb; this page covers the
conversational behavior.

## Hands-free conversation

**The loop.** Opening voice mode (and every reply after it, while
"Listen again after each response" is on) opens the microphone, records
until you stop talking, transcribes, sends, reads the reply aloud, then
listens again. Everything below exists to make that loop feel like
talking to someone rather than operating a recorder.

**Barge-in.** While Faustus is speaking, the microphone (opened once and
kept hot for the whole hands-free session, see `openMic` in `audio.ts`)
keeps being watched for speech via `watchForSpeech`. Energy sustained for
250 ms above `BARGE_IN_THRESHOLD` (deliberately higher than the normal VAD
threshold, to resist the speaker bleeding into the mic even with echo
cancellation on) stops the reply immediately and starts listening on that
same already-open stream — no permission or device round trip, so the
first words are not lost. The status line shows "Go ahead" briefly.

**Echo guard.** An utterance heard within 1.5 s of Faustus's own speech is
compared against what was just spoken (`isEcho` in `engine.ts`, using a
token-overlap `similarity` helper); at ≥ 0.8 similarity it is the mic
re-hearing Faustus, not a real interruption, and is discarded silently —
listening continues. Whisper's own well-known silence hallucinations
("you", "thank you", "thanks for watching", "gracias", any
"Subtítulos…" variant) and anything two characters or shorter are
discarded the same way (`isHallucination`).

**Stop phrases.** An utterance that is *only* a stop phrase — "stop",
"para", "cállate", "silencio", "espera", "shut up", "wait" (`STOP_PHRASES`,
whole-utterance and punctuation/case-insensitive) — silences the current
reply and is never sent to the model; the panel goes back to listening.

**Turn-taking pause.** `TurnDetector`'s default silence-ends-your-turn
window is 900 ms (was 1200 ms), with a 250 ms minimum of actual voice
before a turn can end. "Pause that ends your turn" in Voice options picks
short (600 ms) / normal (900 ms) / long (1500 ms); the choice is
remembered in `faustus_voice_prefs.silenceMs`.

**Wake word (optional, off by default).** "Only answer when I say Faustus
first" arms `stripWakeWord`: every utterance is still transcribed, but
only forwarded when "Faustus" (or a common mishearing — "faustos",
"fausto", "faust us") appears within its first three words; the wake word
is stripped before sending, everything else is discarded and listening
continues. While armed, the panel shows "Waiting for “Faustus”".

**Latency readout.** The panel times end-of-speech → transcript ready →
first audio of the reply, and shows an unobtrusive line like "Heard in
0.8s · first reply audio in 2.1s" once the first reply segment starts
playing. The same line is logged to `console.debug` with a `[voice]`
prefix.

## Preferences

All hands-free choices live together in `localStorage` under
`faustus_voice_prefs`: `continuous` (listen again after each response),
`autoSend` (send without reviewing the transcript), `readAloud`,
`silenceMs` (600 / 900 / 1500) and `wakeWord`. Only the person's own
toggle changes are written back; the panel also turns `continuous` off by
itself on an error or an approval prompt, and that never becomes
tomorrow's default.

## Server

`/api/stt/transcribe` (local faster-whisper, greedy decoding, VAD) and
`/api/tts/synthesize` are unchanged by hands-free mode — all of the above
is client-side policy over the same endpoints.

## Providers

Settings → Voice's provider selects reach `tts_provider` / `stt_provider`
(`src/settings.py`), dispatched in `services/tts/tts_service.py` and
`services/stt/stt_service.py`: `disabled`, `browser` (client-side Web
Speech API), `system` (Windows installed voices, TTS only), `local`
(Kokoro / faster-whisper on this machine), `endpoint:<id>` (an
OpenAI-compatible API), and the two below.

### Piper (fully local, MIT)

`tts_provider = "piper"` runs [Piper](https://github.com/rhasspy/piper)
entirely on this machine — no cloud call, ever. Implementation:
`services/tts/piper_voice.py`.

**Runtime.** Two are supported, python preferred:
- the `piper-tts` PyPI package (`pip install piper-tts`, imported as
  `from piper import PiperVoice`), or
- an engine binary at `data/tts/piper/bin/piper(.exe)`.

Neither ships by default. **Settings → Voice → Local (Piper)** shows an
"Install engine" button when neither is present: it downloads the
platform release archive from Piper's GitHub releases
(`piper_windows_amd64.zip` / `piper_linux_x86_64.tar.gz`) and extracts it
into `data/tts/piper/bin/`. This is an admin-only, logged, outbound
network call (`POST /api/tts/piper/install-binary`).

**Voices.** `.onnx` + `.onnx.json` pairs under `data/tts/piper/voices/`,
named `<locale>-<voice>-<quality>` (e.g. `es_ES-davefx-medium`), which is
also how the download path on the public
[rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) Hugging
Face repo is derived: `es/es_ES/davefx/medium/es_ES-davefx-medium.onnx`.
The Settings picker lists installed voices plus a small recommended
catalogue (es_ES: davefx-medium, sharvard-medium, mls_10246-low,
carlfm-x_low; en_US: lessac-medium, amy-medium, ryan-high; en_GB:
alan-medium) with a "Download" button per entry
(`POST /api/tts/piper/voices/download`, admin-only). `GET
/api/tts/piper/voices` returns both lists plus which runtime is active
(`dependency_installed`, `runtime: "python" | "binary" | null`).

**Synthesis.** `tts_voice` names the voice, `tts_speed` maps to Piper's
`length_scale = 1 / speed`. If the request's language doesn't match the
configured voice's language, the first installed voice for that language
is used instead (`select_voice` in `piper_voice.py`) — the same
language-aware fallback the `system` provider already has.

### Command (advanced)

`tts_provider = "command"` / `stt_provider = "command"` run an
operator-supplied local command instead of a built-in engine — useful for
a personal script or a third-party CLI Faustus has no native integration
for. Settings → Voice shows a text field for the template when this
provider is selected, with the placeholder list below.

`tts_command_template` placeholders: `{input_path}` (text to speak, UTF-8
file), `{output_path}` (the command must write a WAV file here),
`{voice}`, `{speed}`, `{language}`.

```
C:\tools\mytts.exe --text-file {input_path} --out {output_path} --voice {voice} --speed {speed}
```

`stt_command_template` placeholders: `{input_path}` (recorded audio file),
`{language}`. If the template includes `{output_path}`, the command must
write the transcript there; otherwise the transcript is the command's
stdout.

Both run via `subprocess.run` (never a shell — the template is split with
`shlex`, POSIX or Windows quoting rules as appropriate), with a 120s
timeout and temp files cleaned up afterward. Implementation:
`services/tts/command_voice.py`, `services/stt/command_stt.py`.

## Stop phrases

The built-in whole-utterance stop list (`STOP_PHRASES` in
`studio/src/voice/engine.ts`: "stop", "para", "cállate", "silencio",
"espera", "shut up", "wait") can be extended, never replaced, with the
`voice_stop_phrases` setting — a "Stop phrases (one per line)" textarea in
Settings → Voice. It reaches the client through `/api/stt/capabilities`'s
`stop_phrases` field (public, no admin needed, same as the rest of that
endpoint) and is merged in by `isStopPhrase(text, extra)` in
`VoicePanel.tsx`.
