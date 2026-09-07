"""Windows' installed voices: offline CPU TTS, without model downloads or disk audio.

The child receives JSON on stdin, never interpolated PowerShell source. Its
bounded lifetime also keeps a stuck Windows speech engine out of the server.
"""
import base64
import json
import os
import subprocess

_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$stream = New-Object System.IO.MemoryStream
try {
    $voices = @($speaker.GetInstalledVoices() | Where-Object Enabled | ForEach-Object VoiceInfo)
    $voice = $voices | Where-Object Name -eq $request.voice | Select-Object -First 1
    if (-not $voice -and $request.language) {
        $lang = ($request.language -split '-')[0]
        $voice = $voices | Where-Object { $_.Culture.TwoLetterISOLanguageName -eq $lang } | Select-Object -First 1
        if (-not $voice) { throw 'No installed system voice for the requested language' }
    }
    if ($voice) { $speaker.SelectVoice($voice.Name) }
    $speaker.Rate = [Math]::Max(-10, [Math]::Min(10, [int]$request.rate))
    $speaker.SetOutputToWaveStream($stream)
    $speaker.Speak([string]$request.text)
    [Console]::Out.Write([Convert]::ToBase64String($stream.ToArray()))
} finally { $speaker.Dispose(); $stream.Dispose() }
'''


def synthesize_system(text: str, voice: str = "", language: str = "", speed: float = 1.0) -> bytes:
    if os.name != "nt":
        raise RuntimeError("System voices require Windows")
    from pathlib import Path
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    request = json.dumps({"text": text, "voice": voice, "language": language, "rate": round((speed - 1) * 5)}, ensure_ascii=False)
    result = subprocess.run(
        [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")],
        input=request.encode("utf-8"), capture_output=True, timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("System speech unavailable. Install a Windows voice for this language in Windows Settings.")
    return base64.b64decode(result.stdout.strip(), validate=True)
