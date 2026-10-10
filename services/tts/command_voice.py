# services/tts/command_voice.py
"""Command-template TTS provider — runs an operator-supplied local command
(a third-party TTS CLI, a personal script…) instead of a built-in engine.

The template is a plain shell command with placeholders:
  {input_path}  - path to a UTF-8 text file holding what to speak
  {output_path} - path the command must write a WAV file to
  {voice}       - the configured tts_voice
  {speed}       - the configured tts_speed
  {language}    - the request's language, if any
  {marks_path}  - optional: a path the command may write a JSON file of word timings and phonemes to
                  (see `marks.build_marks`: {"words": [{"text", "start_ms", "end_ms", "phonemes"?}], "phonemes":
                  [{"p", "start_ms", "end_ms"}]}); without it the timings are estimated from the audio

Example (Windows): C:\\tools\\mytts.exe --text-file {input_path} --out {output_path} --voice {voice} --speed {speed}

Split with shlex so the template is a normal argv list, never interpolated
into a shell string (no shell=True anywhere here).
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 120
PLACEHOLDERS = ("{input_path}", "{output_path}", "{voice}", "{speed}", "{language}", "{marks_path}")


def _split_template(template: str) -> list:
    """The template as an argv list, never a shell string.

    POSIX: shlex with its own quoting rules. Windows: shlex in non-POSIX
    mode keeps backslash paths intact but also keeps the quotes around a
    quoted argument (`-c "import x"` arrives as `"import x"`, quotes and
    all, which the child then sees literally — seen live as a syntax error
    in the test command); strip one matching pair per argument, which is
    what the OS argument parser would have done."""
    if os.name != "nt":
        return shlex.split(template, posix=True)
    out = []
    for tok in shlex.split(template, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
            tok = tok[1:-1]
        out.append(tok)
    return out


def synthesize_command(text: str, template: str, voice: str = "", speed: float = 1.0, language: str = "") -> Optional[bytes]:
    """Runs `template` with placeholders substituted; returns WAV bytes."""
    return synthesize_command_detailed(text, template, voice, speed, language)[0]


def _read_marks(path: Path) -> Optional[dict]:
    """The optional marks file a command wrote; anything unreadable is simply no marks."""
    try:
        if not path.exists() or path.stat().st_size == 0 or path.stat().st_size > 4 * 1024 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        logger.warning("Command TTS: the marks file is not valid JSON; timings will be estimated")
        return None


def synthesize_command_detailed(text: str, template: str, voice: str = "", speed: float = 1.0, language: str = "") -> Tuple[Optional[bytes], Optional[dict]]:
    """Like `synthesize_command`, plus what the command reported about the speech
    (its `{marks_path}` file, if the template uses it): (wav bytes, marks hints).

    The text is written to a temp file and passed via {input_path} (never
    interpolated into the command line itself, which could otherwise be used
    to smuggle extra arguments into the command). The command must write its
    audio to {output_path}.
    """
    template = (template or "").strip()
    if not template:
        logger.error("Command TTS: no tts_command_template configured")
        return None, None
    if "{output_path}" not in template:
        logger.error("Command TTS: template must include {output_path}")
        return None, None

    in_fd, in_name = tempfile.mkstemp(suffix=".txt")
    out_fd, out_name = tempfile.mkstemp(suffix=".wav")
    os.close(out_fd)
    in_path, out_path = Path(in_name), Path(out_name)
    marks_path: Optional[Path] = None
    if "{marks_path}" in template:
        marks_fd, marks_name = tempfile.mkstemp(suffix=".json")
        os.close(marks_fd)
        marks_path = Path(marks_name)
    try:
        with os.fdopen(in_fd, "w", encoding="utf-8") as f:
            f.write(text)

        rendered = template
        for placeholder, value in (
            ("{input_path}", str(in_path)), ("{output_path}", str(out_path)), ("{marks_path}", str(marks_path or "")),
            ("{voice}", voice or ""), ("{speed}", str(speed)), ("{language}", language or ""),
        ):
            rendered = rendered.replace(placeholder, value)

        cmd = _split_template(rendered)
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=COMMAND_TIMEOUT_S, **kwargs)
        except subprocess.TimeoutExpired:
            logger.error("Command TTS: command timed out after %ss", COMMAND_TIMEOUT_S)
            return None, None
        except OSError as e:
            logger.error("Command TTS: failed to launch command: %s", e)
            return None, None
        if result.returncode != 0:
            logger.error("Command TTS: command exited %d: %s", result.returncode, result.stderr.decode("utf-8", "replace")[:500])
            return None, None
        if not out_path.exists() or out_path.stat().st_size == 0:
            logger.error("Command TTS: command produced no output at %s", out_path)
            return None, None
        return out_path.read_bytes(), (_read_marks(marks_path) if marks_path else None)
    finally:
        in_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        if marks_path:
            marks_path.unlink(missing_ok=True)
