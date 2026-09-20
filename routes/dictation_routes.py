# routes/dictation_routes.py
"""Dictate anywhere: push-to-talk that lands transcribed text in whatever
app has focus, driven by the desktop shell's global hotkey.

Three endpoints, all admin + loopback-only (same gate as the native folder
picker in ``routes/workspace_routes.py``): this server can type into and
read the clipboard of whatever has focus on the machine it runs on, which
is only ever safe to expose to the person sitting at that machine.

  * ``POST /api/dictation/capture-target``       — record the current
    foreground window so a later paste can be re-aimed at it.
  * ``POST /api/dictation/paste``                — deliver text into the
    foreground (or a captured target).
  * ``POST /api/dictation/transcribe-and-paste``  — audio in, STT (with the
    usual hallucination cleanup), paste out; the desktop shell only needs
    to record and post one request.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user
from src.upload_limits import read_upload_limited, STT_MAX_AUDIO_BYTES
from src import dictation_paste

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _require_local_admin(request: Request) -> None:
    """403 unless this is an admin (or single-user mode) calling from the
    Faustus host itself. Dictate-anywhere types into and reads the
    clipboard of whatever has focus on this machine — never safe to expose
    beyond the person sitting at it."""
    owner = get_current_user(request)
    if not owner_is_admin_or_single_user(owner):
        raise HTTPException(status_code=403, detail="Dictate anywhere is admin-only")
    from routes.workspace_routes import _reject_cross_origin

    _reject_cross_origin(request)
    client_host = (request.client.host if request.client else "") or ""
    if client_host not in _LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="Dictate anywhere only works when the caller runs on the Faustus host",
        )


def _unsupported(exc: dictation_paste.DictationUnsupported) -> HTTPException:
    return HTTPException(status_code=501, detail=str(exc))


# The hidden recorder window's page (desktop/dictation.cjs). Loaded at this
# server's own origin — not a data: or file: URL — so it passes the same
# origin check (desktop/policy.cjs's localNavigation) every other Faustus
# window does and can be granted the "media" permission exactly the way
# Studio's own mic already is. It never receives generic IPC: the preload it
# loads (desktop/dictation-recorder-preload.cjs) exposes only start/stop.
_RECORDER_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Faustus dictation</title>
<body style="margin:0;background:#000">
<script>
(function () {
  var bridge = window.dictationRecorder;
  if (!bridge) return; // preload didn't load — nothing this page can do
  var stream = null, recorder = null, chunks = [];
  bridge.onStart(function (target, method) {
    chunks = [];
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
      stream = s;
      recorder = new MediaRecorder(s, { mimeType: 'audio/webm' });
      recorder.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      recorder.onstop = function () {
        var blob = new Blob(chunks, { type: 'audio/webm' });
        var form = new FormData();
        form.append('file', blob, 'dictation.webm');
        form.append('method', method || 'clipboard');
        if (target && target.handle) {
          form.append('target_handle', String(target.handle));
          form.append('target_title', target.title || '');
        }
        fetch('/api/dictation/transcribe-and-paste', { method: 'POST', credentials: 'same-origin', body: form })
          .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; }); })
          .then(function (result) { bridge.reportResult(result); })
          .catch(function (err) { bridge.reportResult({ ok: false, status: 0, body: { detail: String(err) } }); })
          .finally(function () { if (stream) stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; });
      };
      recorder.start();
      bridge.reportResult({ ok: true, recording: true });
    }).catch(function (err) {
      bridge.reportResult({ ok: false, status: 0, body: { detail: 'Microphone: ' + String(err) } });
    });
  });
  bridge.onStop(function () {
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    else bridge.reportResult({ ok: false, status: 0, body: { detail: 'Not recording' } });
  });
})();
</script>
</body>
"""


def setup_dictation_routes(stt_service) -> APIRouter:
    router = APIRouter(prefix="/api/dictation", tags=["dictation"])

    @router.get("/recorder", response_class=HTMLResponse)
    async def recorder_page(request: Request):
        # A top-level page load, not a fetch/XHR, so the Sec-Fetch-Site/
        # Origin cross-origin check (_reject_cross_origin) does not apply
        # here — a normal browser navigation from the Electron shell sends
        # neither header. Admin + loopback still gate it.
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Dictate anywhere is admin-only")
        client_host = (request.client.host if request.client else "") or ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="Local only")
        return HTMLResponse(_RECORDER_HTML)

    @router.post("/capture-target")
    async def capture_target(request: Request):
        _require_local_admin(request)
        try:
            target = await run_in_threadpool(dictation_paste.capture_foreground)
        except dictation_paste.DictationUnsupported as exc:
            raise _unsupported(exc) from exc
        except dictation_paste.DictationError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return target.as_dict()

    @router.post("/paste")
    async def paste(request: Request):
        _require_local_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        text = str(body.get("text") or "")
        if not text.strip():
            raise HTTPException(status_code=400, detail="text is required")
        method = str(body.get("method") or "clipboard")
        target = None
        if body.get("target"):
            try:
                target = dictation_paste.ForegroundTarget.from_dict(body["target"])
            except dictation_paste.DictationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            result = await run_in_threadpool(
                dictation_paste.paste_text, text, target=target, method=method,
            )
        except dictation_paste.DictationUnsupported as exc:
            raise _unsupported(exc) from exc
        except dictation_paste.DictationError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return result

    @router.post("/transcribe-and-paste")
    async def transcribe_and_paste(
        request: Request,
        file: UploadFile = File(...),
        method: str = Form("clipboard"),
        language: str = Form("", max_length=20, pattern=r"^[a-zA-Z-]*$"),
        target_handle: str = Form(""),
        target_title: str = Form(""),
    ):
        _require_local_admin(request)
        if not dictation_paste.IS_WINDOWS:
            raise _unsupported(dictation_paste.DictationUnsupported())
        if not stt_service.available:
            raise HTTPException(status_code=503, detail="STT unavailable. Configure a voice provider first.")

        audio_bytes = await read_upload_limited(file, STT_MAX_AUDIO_BYTES, "Audio file")
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Empty audio file")

        target = None
        if target_handle.strip():
            try:
                target = dictation_paste.ForegroundTarget(handle=int(target_handle), title=target_title)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="target_handle must be an integer") from exc

        def run():
            kwargs = {"language_override": language} if language else {}
            text = stt_service.transcribe(audio_bytes, **kwargs)
            if not text or not text.strip():
                return {"text": "", "pasted": False}
            result = dictation_paste.paste_text(text, target=target, method=method)
            return {"text": text, "pasted": True, **result}

        try:
            return await run_in_threadpool(run)
        except dictation_paste.DictationUnsupported as exc:
            raise _unsupported(exc) from exc
        except dictation_paste.DictationError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("transcribe-and-paste failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Transcription failed. Check the speech provider and retry.") from exc

    return router
