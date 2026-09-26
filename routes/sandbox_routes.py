"""GET /api/sandbox/app — the runner page a chat's runnable block loads into.

A reply's ```html``` block runs in an iframe. As srcdoc it inherited Studio's
own content policy (scripts only with Studio's nonce), so no generated app's
script ever ran. The runner is a tiny page served with a policy of its own:
its inline code may run, nothing may load from the network, and the CSP
`sandbox` directive gives it an opaque origin even if someone opens it on its
own, so it can never read Faustus's cookies or storage. The parent posts the
app's HTML to it once it says it is ready; it writes that in place.
(core/middleware.py sets the headers; see `is_sandbox_app` there.)
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

RUNNER_CSP = ("sandbox allow-scripts allow-modals allow-forms; default-src 'none'; "
              "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
              "font-src data:; media-src data: blob:; frame-ancestors 'self'")

RUNNER_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>app</title></head><body>
<script>
(function () {
  if (window.parent === window) return;
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || typeof d.faustusApp !== 'string') return;
    document.open(); document.write(d.faustusApp); document.close();
  }, { once: true });
  window.parent.postMessage({ faustusAppReady: true }, '*');
})();
</script></body></html>"""


def setup_sandbox_routes() -> APIRouter:
    router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])

    @router.get("/app")
    async def sandbox_app():
        return HTMLResponse(RUNNER_HTML, headers={"Cache-Control": "no-store"})

    return router
