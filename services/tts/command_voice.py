# services/tts/command_voice.py
"""Command-template TTS provider — runs an operator-supplied local command
(a third-party TTS CLI, a personal script…) instead of a built-in engine.

The template is a plain shell command with placeholders:
  {input_path}  - path to a UTF-8 text file holding what to speak
  {output_path} - path the command must write a WAV file to
  {voice}       - the configured tts_voice
  {speed}       - the configured tts_speed
  {language}    - the request's language, if any

Example (Windows): C:\\tools\\mytts.exe --text-file {input_path} --out {output_path} --voice {voice} --speed {speed}

Split with shlex so the template is a normal argv list, never interpolated
into a shell string (no shell=True anywhere here).
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 120
PLACEHOLDERS = ("{input_path}", "{output_path}", "{voice}", "{speed}", "{language}")


def _split_template(template: str) -> list:
    # shlex.split(posix=False) keeps Windows-style backslash paths intact;
    # POSIX keeps its own quoting/escaping rules.
    return shlex.split(template, posix=(os.name != "nt"))


def synthesize_command(text: str, template: str, voice: str = "", speed: float = 1.0, language: str = "") -> Optional[bytes]:
    """Runs `template` with placeholders substituted; returns WAV bytes.

    The text is written to a temp file and passed via {input_path} (never
    interpolated into the command line itself, which could otherwise be used
    to smuggle extra arguments into the command). The command must write its
    audio to {output_path}.
    """
    template = (template or "").strip()
    if not template:
        logger.error("Command TTS: no tts_command_template configured")
        return None
    if "{output_path}" not in template:
        logger.error("Command TTS: template must include {output_path}")
        return None

    in_fd, in_name = tempfile.mkstemp(suffix=".txt")
    out_fd, out_name = tempfile.mkstemp(suffix=".wav")
    os.close(out_fd)
    in_path, out_path = Path(in_name), Path(out_name)
    try:
        with os.fdopen(in_fd, "w", encoding="utf-8") as f:
            f.write(text)

        rendered = template
        for placeholder, value in (
            ("{input_path}", str(in_path)), ("{output_path}", str(out_path)),
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
            return None
        except OSError as e:
            logger.error("Command TTS: failed to launch command: %s", e)
            return None
        if result.returncode != 0:
            logger.error("Command TTS: command exited %d: %s", result.returncode, result.stderr.decode("utf-8", "replace")[:500])
            return None
        if not out_path.exists() or out_path.stat().st_size == 0:
            logger.error("Command TTS: command produced no output at %s", out_path)
            return None
        return out_path.read_bytes()
    finally:
        in_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
