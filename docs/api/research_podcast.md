# Research podcast

A finished Deep Research report can be turned into a two-voice audio episode:
a dialogue script written by a model from the report, spoken by two local
Piper voices. Code: `src/research_podcast.py`; routes in
`routes/research/research_routes.py`; Studio panel
`studio/src/screens/research/Podcast.tsx`; agent tool `research_podcast`
(`src/agent_tools/research_podcast_tools.py`).

## How it works

1. **Preflight** (before any model call). Piper must have a runtime
   (`services/tts/piper_voice.active_runtime()`) and at least one installed
   voice in the report's language (`report_language` of the research JSON,
   detected from the text when missing). Without one, the start request fails
   with a message that names catalogue voices to download in
   Settings → Voice → Local (Piper).
2. **Script.** The report (`raw_report`, else `result`) is cleaned of its
   source list, citation markers and URLs. A report over 2,200 words is first
   condensed: its sections are grouped into chunks of at most 1,400 words and
   each chunk becomes faithful notes in one short model call, so a long report
   fits a local model. One more call writes the dialogue as JSON
   `{"lines": [{"speaker": "A"|"B", "text": "..."}]}` in the report's
   language, sized to `research_podcast_minutes` (about 150 words a minute).
   The prompt keeps the dialogue inside the report: no fact, figure, name or
   opinion that the report does not carry, uncertainty stated as such, sources
   named sparingly and never read as URLs or citation numbers.
   The model is the utility endpoint, falling back to the default one.
3. **Parsing.** The reply is never trusted as JSON: think blocks and code
   fences are removed, trailing commas repaired, complete items salvaged from
   a truncated reply, and a plain `A: …` transcript accepted. Speakers
   `Host A`, `speaker b`, `1`/`2` or two names map to A/B. Markup, citation
   markers, URLs and bracketed stage directions are removed from each line.
   A script needs at least four lines and both speakers; otherwise the model
   is asked once more, and a second failure fails the job.
4. **Voices.** Two distinct installed voices of the language, best quality
   first (`research_podcast_voice_a` / `_b` override the pick when they are
   installed and speak the language; otherwise they are ignored with a
   warning). With only one voice installed both hosts use it, host B slightly
   faster, and a warning names a second voice to download.
5. **Audio.** Each line is synthesized on its own (long lines are split at
   sentence boundaries, at most 280 characters per Piper call, because the
   Piper worker stops after 60 s), in a worker thread so the event loop is
   never blocked. Pauses go between lines (longer at a change of speaker) and
   the clips are joined with Python's `wave` module. A clip in a different
   sample format is converted with ffmpeg; without ffmpeg that is an error
   naming the fix. With `research_podcast_format` = `mp3` and ffmpeg present
   the result is encoded to MP3 (96 kbit/s); without ffmpeg the WAV is kept
   and a warning says so.
6. **Keep.** Audio and a Markdown transcript go to the artifact store
   (`skill_id` `research.podcast`, owner = the report's owner), and a
   `podcast` block is merged into the research JSON with
   `atomic_write_json`; every other key of the file is left as it was.

The job lives in memory. If the server restarts while it runs, the next
status read turns the block still marked `running` into `failed` with a
message saying so; nothing half-made is kept.

## Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `research_podcast_minutes` | `6` | Target length of the episode (1–30). |
| `research_podcast_voice_a` | `""` | Installed Piper voice for host A; empty = automatic pick. |
| `research_podcast_voice_b` | `""` | Installed Piper voice for host B; empty = automatic pick. |
| `research_podcast_format` | `"mp3"` | `mp3` (needs ffmpeg, falls back to WAV) or `wav`. |

## Routes

All routes require the report's owner; another user's report answers 404.

### `POST /api/research/{id}/podcast`

Starts the job and returns the status payload below with `status: "running"`.
`409` while a job for the report is already running; `400` with an
actionable `detail` when Piper or a voice is missing or the report has no
text. Starting again after a finished podcast makes a new one.

### `GET /api/research/{id}/podcast`

```json
{
  "session_id": "…",
  "status": "none | running | done | failed",
  "progress": {"phase": "condensing | script | synthesizing | mixing | encoding | saving",
               "lines_done": 12, "lines_total": 40, "chunks_done": 0, "chunks_total": 0,
               "started_at": 1790000000.0, "language": "es"},
  "script": [{"speaker": "A", "text": "…"}],
  "warnings": ["…"],
  "error": "",
  "podcast": {"status": "done", "artifact_id": "occ_…", "transcript_artifact_id": "occ_…",
              "voices": {"A": "es_ES-davefx-medium", "B": "es_ES-sharvard-medium"},
              "speeds": {"A": 1.0, "B": 1.0}, "duration_s": 372.4, "lines": 42,
              "format": "mp3", "media_type": "audio/mpeg", "language": "es",
              "warnings": [], "created_at": 1790000000.0, "error": ""},
  "audio_url": "/api/research/{id}/podcast/audio",
  "download_url": "/api/research/{id}/podcast/audio?download=1",
  "artifact_url": "/api/artifacts/{artifact_id}/download",
  "transcript_url": "/api/artifacts/{transcript_artifact_id}/download"
}
```

`progress` is `null` unless the job is running; the link fields appear only
when `status` is `done`.

### `GET /api/research/{id}/podcast/audio`

Streams the finished audio inline (for `<audio src>`); `?download=1` sends it
as an attachment. `404` until a podcast is done or when its artifact is gone.

## Research JSON

The `podcast` block of `DATA_DIR/deep_research/<id>.json` holds the fields
shown under `podcast` above plus the `script`. While running it is
`{"status": "running", "started_at", "language", "error": ""}`; a failure
is `{"status": "failed", "error", "created_at", "warnings"}`.

## Where it shows

- Research screen: a **Podcast** button in a finished report's footer (next to
  Export) and in the recent reports list opens the panel and starts the job
  when there is no podcast yet; the panel shows the phase and a progress bar,
  then the player, the audio and transcript downloads, and the script.
- Library → Research: **Podcast** in a report's actions menu, and the panel in
  the expanded report.
- The visual report (`/api/research/report/{id}`) ends with a player when a
  podcast is done.

## Agent tool

`research_podcast` `{"research_id": "…", "action": "start" | "status",
"regenerate": false}`. `start` begins the job and returns immediately (an
existing finished podcast is returned instead unless `regenerate` is true);
`status` reports progress and, when done, the audio link. Only the caller's
own reports are reachable. Effect class `write_private`.
