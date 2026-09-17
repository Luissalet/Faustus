# services/stt/command_stt.py
"""Command-template STT provider — runs an operator-supplied local command
against a recorded audio file and reads back the transcript.

Placeholders:
  {input_path}  - path to the recorded audio (webm/ogg/mp4, whatever was captured)
  {output_path} - present only if the template includes it: the command must
                   write the transcript there instead of printing it
  {language}    - the request's language, if any (empty for auto-detect)
  {voice}, {speed} - accepted for template-shape symmetry with TTS; empty here

When {output_path} is not in the template, the transcript is the command's
stdout (stripped). Split with shlex, no shell=True.
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


def _split_template(template: str) -> list:
    return shlex.split(template, posix=(os.name != "nt"))


def transcribe_command(audio_bytes: bytes, template: str, language: str = "") -> Optional[str]:
    template = (template or "").strip()
    if not template:
        logger.error("Command STT: no stt_command_template configured")
        return None
    writes_output = "{output_path}" in template

    in_fd, in_name = tempfile.mkstemp(suffix=".webm")
    in_path = Path(in_name)
    out_path: Optional[Path] = None
    try:
        with os.fdopen(in_fd, "wb") as f:
            f.write(audio_bytes)

        rendered = template
        if writes_output:
            out_fd, out_name = tempfile.mkstemp(suffix=".txt")
            os.close(out_fd)
            out_path = Path(out_name)
            rendered = rendered.replace("{output_path}", str(out_path))
        rendered = (
            rendered.replace("{input_path}", str(in_path))
            .replace("{language}", language or "")
            .replace("{voice}", "").replace("{speed}", "")
        )

        cmd = _split_template(rendered)
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=COMMAND_TIMEOUT_S, **kwargs)
        except subprocess.TimeoutExpired:
            logger.error("Command STT: command timed out after %ss", COMMAND_TIMEOUT_S)
            return None
        except OSError as e:
            logger.error("Command STT: failed to launch command: %s", e)
            return None
        if result.returncode != 0:
            logger.error("Command STT: command exited %d: %s", result.returncode, result.stderr.decode("utf-8", "replace")[:500])
            return None

        if writes_output and out_path is not None:
            if not out_path.exists():
                logger.error("Command STT: command produced no transcript at %s", out_path)
                return None
            return out_path.read_text(encoding="utf-8", errors="replace").strip()
        return result.stdout.decode("utf-8", "replace").strip()
    finally:
        in_path.unlink(missing_ok=True)
        if out_path is not None:
            out_path.unlink(missing_ok=True)
