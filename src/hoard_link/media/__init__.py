"""Media commons: finding and running ffmpeg / yt-dlp, probing, subtitles, speech to text.

Importing this package needs only the standard library; every optional dependency (``numpy``, ``faster_whisper``,
``imageio_ffmpeg``) is imported inside the function that uses it.

* :mod:`hoard_link.media.bins` — find / verify / update the external programs (``ffmpeg``, ``ffprobe``, ``yt-dlp``, ``gallery-dl``,
  ``piper``, ``node``) and build yt-dlp arguments.
* :mod:`hoard_link.media.ffmpeg` — :class:`FFmpeg`: probe, run with progress and cancellation, PCM / frames / waveform,
  loudness, encoders, ``ensure_playable``, WAV helpers, filter escaping.
* :mod:`hoard_link.media.subs` — subtitle cues: SRT / VTT / LRC / ASS writers and parsers.
* :mod:`hoard_link.media.stt` — faster-whisper transcriber with a GPU lease, CPU fallback and hallucination filter.

The Node twin of the pure parts is ``js/hoard-commons/media.js``.
"""

from __future__ import annotations

__all__ = ["bins", "ffmpeg", "subs", "stt"]
