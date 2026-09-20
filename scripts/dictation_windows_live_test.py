#!/usr/bin/env python
"""Windows live test for "dictate anywhere" — needs no microphone.

Run this ON the Windows machine, with the Faustus server already running
(``python server_runtime.py start --port 7000 --owner desktop`` or just the
desktop app open) and logged in as an admin. It talks to the real
``/api/dictation/*`` routes and Win32 APIs directly:

  1. Opens Notepad.
  2. Puts a sentinel string on the clipboard.
  3. Captures Notepad as the dictation target (``POST /api/dictation/capture-target``,
     called while Notepad is foreground).
  4. Calls ``paste_text`` directly (no HTTP — importing ``src.dictation_paste``
     in-process, same as the route does) to paste a fixed string into Notepad.
  5. Separately, synthesizes a WAV with the Piper voice
     ``es_ES-davefx-medium`` (must already be installed — see
     ``services/tts/piper_voices.py`` / Settings > Voice > Piper) and posts
     it to ``POST /api/dictation/transcribe-and-paste`` to exercise the full
     STT -> cleanup -> paste pipeline.
  6. Reads Notepad's text back via the Win32 API (``GetWindowText`` on its
     edit control, found through UI Automation / ``EnumChildWindows`` since
     modern Notepad's edit control has no fixed class name across builds)
     and asserts both dictated strings landed.
  7. Asserts the sentinel is back on the clipboard (nothing else overwrote
     it after the last restore).
  8. Closes Notepad without saving.

Nothing here is exercised by the (non-Windows) CI test suite — those cover
the same logic (paste_text, transcribe-and-paste) against fakes in
``tests/test_dictation_paste.py`` / ``tests/test_dictation_routes.py``. This
script is the fill-in for the parts that only a real Windows session can
prove: a real foreground window, a real clipboard, a real Piper voice.

Usage:
    python scripts/dictation_windows_live_test.py --base-url http://127.0.0.1:7000

Exit code 0 = every assertion passed. Any failure raises and Notepad is
still closed (best-effort) in a ``finally`` block.
"""
from __future__ import annotations

import argparse
import ctypes
import io
import subprocess
import sys
import time
import wave
from ctypes import wintypes

if sys.platform != "win32":
    sys.exit("This script only runs on Windows.")

import httpx  # noqa: E402  (after the platform guard on purpose)

sys.path.insert(0, ".")
from src import dictation_paste as dp  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SENTINEL = "FAUSTUS_DICTATION_LIVE_TEST_SENTINEL_9f3a"
PASTE_METHOD_TEXT = "Este es el texto pegado por dictado en cualquier lugar."
STT_SENTENCE = "Buenas tardes, esta es una prueba de dictado en cualquier lugar."


# ── small Win32 helpers (independent of src.dictation_paste's WindowsBackend,
#    on purpose — this script must catch a regression in that module too) ──

def _launch_notepad() -> subprocess.Popen:
    proc = subprocess.Popen(["notepad.exe"])
    for _ in range(100):
        if _find_window_by_process(proc.pid):
            return proc
        time.sleep(0.1)
    raise RuntimeError("Notepad did not open in time")


def _find_window_by_process(pid: int) -> int:
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def _edit_control(notepad_hwnd: int) -> int:
    """Modern Notepad nests its edit control several levels deep; walk
    EnumChildWindows and take the first control that responds to WM_GETTEXT
    with a non-trivial class (Notepad's own is always a single child tree)."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if "edit" in cls.value.lower():
            found.append(hwnd)
        return True

    user32.EnumChildWindows(notepad_hwnd, callback, 0)
    if not found:
        raise RuntimeError("Could not find Notepad's edit control")
    return found[-1]


def _read_notepad_text(edit_hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(edit_hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(edit_hwnd, buf, length + 1)
    return buf.value


def _close_notepad_without_saving(proc: subprocess.Popen, hwnd: int) -> None:
    # Discard unsaved changes: Ctrl+Shift+S / Alt+F4 pops a save dialog in
    # recent Notepad builds even for a title-less untouched instance is
    # unpredictable, so just kill the process outright — nothing here was
    # ever meant to be saved.
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"], check=False)


def _synthesize_piper_wav(text: str) -> bytes:
    """Uses the already-installed Piper voice es_ES-davefx-medium via the
    ``piper`` CLI, the same one Faustus's own Piper TTS provider shells out
    to (services/tts/piper_voice.py). Raises if the voice isn't installed —
    that's a setup problem this script should surface, not paper over."""
    proc = subprocess.run(
        ["piper", "--model", "es_ES-davefx-medium", "--output-raw"],
        input=text.encode("utf-8"), capture_output=True, check=True, timeout=60,
    )
    pcm = proc.stdout
    if not pcm:
        raise RuntimeError("piper produced no audio — is es_ES-davefx-medium installed?")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(pcm)
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:7000")
    parser.add_argument("--cookie", default="", help="Session cookie header value, if auth is enabled")
    args = parser.parse_args()

    headers = {"Cookie": args.cookie} if args.cookie else {}
    client = httpx.Client(base_url=args.base_url, headers=headers, timeout=30)

    proc = None
    notepad_hwnd = 0
    try:
        # 1. Open Notepad.
        proc = _launch_notepad()
        notepad_hwnd = _find_window_by_process(proc.pid)
        edit_hwnd = _edit_control(notepad_hwnd)
        user32.SetForegroundWindow(notepad_hwnd)
        time.sleep(0.3)

        # 2. Sentinel on the clipboard.
        real_clipboard = dp.Win32Clipboard()
        real_clipboard.write_text(SENTINEL)
        assert real_clipboard.read_text() == SENTINEL

        # 3. Capture Notepad as the target (via the HTTP route, exercising
        #    the real server-side capture — Notepad must be foreground now).
        response = client.post("/api/dictation/capture-target")
        response.raise_for_status()
        target_data = response.json()
        assert target_data["handle"] == notepad_hwnd, (
            f"captured {target_data} but Notepad's hwnd is {notepad_hwnd}"
        )
        target = dp.ForegroundTarget.from_dict(target_data)

        # 4. paste_text() directly, in-process (what the route calls).
        result = dp.paste_text(PASTE_METHOD_TEXT, target=target, method="clipboard")
        time.sleep(0.3)
        text_after_paste = _read_notepad_text(edit_hwnd)
        assert PASTE_METHOD_TEXT in text_after_paste, (
            f"expected {PASTE_METHOD_TEXT!r} in Notepad, got {text_after_paste!r}"
        )
        assert result["clipboard_restored"] is True
        assert real_clipboard.read_text() == SENTINEL, "clipboard was not restored after paste_text"
        print("[ok] paste_text landed in Notepad and restored the clipboard sentinel")

        # 5. Full transcribe-and-paste pipeline with a Piper-synthesized WAV.
        user32.SetForegroundWindow(notepad_hwnd)  # re-focus, paste_text() may have moved focus around
        time.sleep(0.2)
        target_data = client.post("/api/dictation/capture-target").json()
        wav_bytes = _synthesize_piper_wav(STT_SENTENCE)
        files = {"file": ("dictation.wav", wav_bytes, "audio/wav")}
        data = {
            "method": "clipboard",
            "target_handle": str(target_data["handle"]),
            "target_title": target_data.get("title", ""),
        }
        response = client.post("/api/dictation/transcribe-and-paste", files=files, data=data)
        response.raise_for_status()
        pipeline_result = response.json()
        assert pipeline_result["pasted"] is True, pipeline_result
        transcript = pipeline_result["text"]
        print(f"[info] STT transcribed: {transcript!r}")
        time.sleep(0.3)
        text_after_pipeline = _read_notepad_text(edit_hwnd)
        assert transcript.strip() and transcript.strip() in text_after_pipeline, (
            f"transcript {transcript!r} not found in Notepad text {text_after_pipeline!r}"
        )

        # 6/7. Final clipboard-restore check after the full pipeline.
        assert real_clipboard.read_text() == SENTINEL, "clipboard was not restored after transcribe-and-paste"
        print("[ok] transcribe-and-paste pipeline landed real STT output and restored the clipboard")

        print("ALL ASSERTIONS PASSED")
        return 0
    finally:
        # 8. Close Notepad without saving, regardless of pass/fail above.
        if proc is not None:
            _close_notepad_without_saving(proc, notepad_hwnd)


if __name__ == "__main__":
    raise SystemExit(main())
