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
