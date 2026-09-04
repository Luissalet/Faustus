"""
tests/test_comfyui_backend.py — the client, against a real HTTP server.

Not mocks. A `ThreadingHTTPServer` on a real port speaks ComfyUI's protocol —
`/system_stats`, `/object_info`, `/prompt`, `/history/{id}`, `/queue`,
`/interrupt`, `/view` — with the shapes ComfyUI actually returns, including
the awkward ones: history entries keyed by prompt id, queue entries as
positional lists, a `completed: false` that means failure, and messages as
`[name, payload]` pairs.

That matters because every bug this client can have lives in the HTTP layer:
a URL built wrong, a response shape read wrong, a cancel that only interrupts.
None of those show up against a mock that returns what the test author
imagined.

The two behaviours worth the whole file:

* **it refuses before it queues.** A template asking for a checkpoint the
  engine does not have is answered in a second, naming the file, instead of
  becoming a job that dies inside the sampler twenty minutes later;
* **a cancel does both halves.** Interrupt stops what is running; a job still
  in the queue has to be deleted. Doing only the first is the bug that makes
  cancel look ignored — the job just starts a moment later.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from src.media_backends import ComfyUIBackend, ComfyUIError


class FakeComfy:
    """A real server that answers like ComfyUI. `state` is what the test
    drives; `calls` is what the client actually asked for, which is how the
    "did it do both halves of a cancel" test can tell."""

    def __init__(self):
        self.checkpoints = ["sd_xl_base_1.0.safetensors", "dreamshaper_8.safetensors"]
        self.nodes = ["CheckpointLoaderSimple", "CLIPTextEncode", "EmptyLatentImage",
                      "KSampler", "VAEDecode", "SaveImage", "LoadImage", "VAEEncode"]
        self.history = {}
        self.running = []
        self.pending = []
        self.node_errors = {}
        self.files = {"faustus/product_00001_.png": b"\x89PNG\r\n\x1a\nfake-bytes"}
        self.calls = []
        self.submitted = []
        self._server = None
        self._thread = None

    # -- lifecycle ---------------------------------------------------------
    def start(self, port: int = 0):
        """Port 0 for tests (never collides); a real port when something
        outside the test process has to reach it — the live probe binds 8188,
        where the running Faustus looks for ComfyUI by default."""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):          # keep the test output readable
                pass

            def _send(self, payload, code=200, raw=False):
                body = payload if raw else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type",
                                 "application/octet-stream" if raw else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                outer.calls.append(("GET", url.path))
                if url.path == "/system_stats":
                    return self._send({"system": {"comfyui_version": "0.3.0"},
                                       "devices": [{"name": "NVIDIA GeForce RTX 4070 Ti",
                                                    "vram_total": 12 * 1024 ** 3}]})
                if url.path == "/object_info":
                    return self._send({
                        name: ({"input": {"required": {"ckpt_name": [outer.checkpoints]}}}
                               if name == "CheckpointLoaderSimple" else {"input": {}})
                        for name in outer.nodes})
                if url.path.startswith("/history/"):
                    wanted = url.path.rsplit("/", 1)[-1]
                    entry = outer.history.get(wanted)
                    return self._send({wanted: entry} if entry else {})
                if url.path == "/queue":
                    return self._send({"queue_running": outer.running,
                                       "queue_pending": outer.pending})
                if url.path == "/view":
                    q = parse_qs(url.query)
                    key = "/".join(x for x in [q.get("subfolder", [""])[0],
                                               q.get("filename", [""])[0]] if x)
                    blob = outer.files.get(key)
                    if blob is None:
                        return self._send({"error": "not found"}, code=404)
                    return self._send(blob, raw=True)
                return self._send({"error": "no route"}, code=404)

            def do_POST(self):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.calls.append(("POST", url.path, body))
                if url.path == "/prompt":
                    if outer.node_errors:
                        return self._send({"node_errors": outer.node_errors})
                    outer.submitted.append(body)
                    pid = f"p{len(outer.submitted)}"
                    outer.pending.append([len(outer.submitted), pid, body.get("prompt")])
                    return self._send({"prompt_id": pid, "number": len(outer.submitted),
                                       "node_errors": {}})
                if url.path == "/queue":
                    for gone in body.get("delete") or []:
                        outer.pending = [p for p in outer.pending if p[1] != gone]
                    return self._send({})
                if url.path == "/interrupt":
                    outer.running = []
                    return self._send({})
                return self._send({"error": "no route"}, code=404)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # -- driving it --------------------------------------------------------
    def finish(self, prompt_id, *, images=("faustus/product_00001_.png",)):
        """Finish a job with outputs named the way ComfyUI names them.

        A `filename_prefix` of `faustus/product` produces
        `{"subfolder": "faustus", "filename": "product_00001_.png"}` — the
        folder and the name arrive as separate fields, and `/view` needs both.
        Getting that split wrong in the fake is how a client that cannot
        download anything passes its tests."""
        self.pending = [p for p in self.pending if p[1] != prompt_id]
        entries = []
        for path in images:
            subfolder, _, name = path.rpartition("/")
            entries.append({"filename": name, "subfolder": subfolder,
                            "type": "output"})
            self.files.setdefault(path, b"\x89PNG\r\n\x1a\nfake-bytes")
        self.history[prompt_id] = {
            "prompt": [], "status": {"completed": True, "status_str": "success",
                                     "messages": []},
            "outputs": {"7": {"images": entries}}}

    def fail(self, prompt_id, *, node="KSampler", why="CUDA out of memory"):
        self.pending = [p for p in self.pending if p[1] != prompt_id]
        self.history[prompt_id] = {
            "prompt": [], "outputs": {},
            "status": {"completed": False, "status_str": "error", "messages": [
                ["execution_start", {}],
                ["execution_error", {"node_type": node, "exception_message": why}]]}}

    def interrupt(self, prompt_id):
        """What a REAL ComfyUI records when somebody stops a running job.

        Identical to a failure except for the message name — `completed:
        false` and `status_str: error` both. That is exactly why the client
        has to read the message: without it, every cancel reads as a crash.
        Copied from the real engine after it did this to a live render."""
        self.running = []
        self.pending = [p for p in self.pending if p[1] != prompt_id]
        self.history[prompt_id] = {
            "prompt": [], "outputs": {},
            "status": {"completed": False, "status_str": "error", "messages": [
                ["execution_start", {}],
                ["execution_interrupted", {"node_id": "5", "node_type": "KSampler"}]]}}

    def start_running(self, prompt_id):
        self.pending = [p for p in self.pending if p[1] != prompt_id]
        self.running = [[1, prompt_id, {}]]


@pytest.fixture()
def engine():
    fake = FakeComfy()
    url = fake.start()
    try:
        yield fake, ComfyUIBackend(url)
    finally:
        fake.stop()


PLAN = {
    "graph": {"1": {"class_type": "CheckpointLoaderSimple",
                    "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
              "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0]}}},
    "models": [{"name": "sd_xl_base_1.0.safetensors", "kind": "checkpoint"}],
    "values": {"seed": 42},
}


# ── is it there ───────────────────────────────────────────────────────────

def test_a_running_engine_with_a_model_is_ready_and_says_what_it_is_on(engine):
    fake, comfy = engine
    gate = comfy.probe()
    assert gate["ok"] and gate["reason"] == "ready"
    assert "RTX 4070 Ti" in gate["detail"] and "12.0 GB" in gate["detail"]
    assert "2 checkpoint" in gate["detail"]


def test_nothing_listening_is_not_running_and_says_how_to_point_at_it():
    """The most common state of this backend, and it deserves the answer
    "start ComfyUI" rather than "an error occurred"."""
    gate = ComfyUIBackend("http://127.0.0.1:1").probe()
    assert gate["ok"] is False
    assert gate["reason"] == "backend_unavailable"
    assert "Start ComfyUI" in gate["detail"] and "COMFYUI_URL" in gate["detail"]


def test_running_with_no_checkpoint_is_unavailable_with_the_fix_in_the_sentence(engine):
    """Reads harsh, is honest: a render sent to an engine with no model fails
    inside the sampler, and the message ComfyUI gives then is about a dropdown
    value, not about a missing file."""
    fake, comfy = engine
    fake.checkpoints = []
    gate = comfy.probe()
    assert gate["ok"] is False and gate["reason"] == "no_models"
    assert "models/checkpoints" in gate["detail"]
    assert "does not download models" in gate["detail"]


# ── it checks before it queues ────────────────────────────────────────────

def test_a_checkpoint_the_engine_does_not_have_is_refused_by_name(engine):
    fake, comfy = engine
    plan = {**PLAN, "models": [{"name": "flux1-dev.safetensors", "kind": "checkpoint"}]}
    with pytest.raises(ComfyUIError) as err:
        comfy.submit(plan)
    assert err.value.reason == "missing_requirements"
    assert "flux1-dev.safetensors" in err.value.detail
    # and it says what IS there, because the usual cause is a spelling
    assert "sd_xl_base_1.0.safetensors" in err.value.detail
    assert fake.submitted == [], "it queued a job it knew would fail"


def test_a_node_the_engine_does_not_have_is_refused_and_faustus_will_not_install_it(engine):
    fake, comfy = engine
    plan = {**PLAN, "graph": {**PLAN["graph"],
                              "9": {"class_type": "AnimateDiffLoader", "inputs": {}}}}
    with pytest.raises(ComfyUIError) as err:
        comfy.submit(plan)
    assert "AnimateDiffLoader" in err.value.detail
    assert "does not install custom nodes" in err.value.detail
    assert fake.submitted == []


def test_a_plan_with_no_graph_is_refused_before_anything_is_asked(engine):
    fake, comfy = engine
    with pytest.raises(ComfyUIError) as err:
        comfy.submit({"models": []})
    assert err.value.reason == "empty_graph"
    assert fake.calls == [], "it went to the network for a plan that was empty"


# ── running one ───────────────────────────────────────────────────────────

def test_a_submitted_job_gets_an_id_and_the_graph_arrives_as_sent(engine):
    fake, comfy = engine
    job = comfy.submit(PLAN)
    assert job["prompt_id"] == "p1"
    assert fake.submitted[0]["prompt"] == PLAN["graph"]
    assert fake.submitted[0]["client_id"] == "faustus"


def test_the_engine_rejecting_the_graph_comes_back_with_its_own_words(engine):
    """The template is ours, so a validation error here is a bug in a file we
    control. Passing ComfyUI's node/field detail through beats "the render
    failed"."""
    fake, comfy = engine
    fake.node_errors = {"5": {"errors": [{"message": "value not in list",
                                          "details": "sampler_name: 'nope'"}]}}
    with pytest.raises(ComfyUIError) as err:
        comfy.submit(PLAN)
    assert err.value.reason == "rejected_by_engine"
    assert "sampler_name" in err.value.detail


def test_a_job_reports_queued_then_running_then_completed(engine):
    fake, comfy = engine
    job = comfy.submit(PLAN)
    pid = job["prompt_id"]

    queued = comfy.status(pid)
    assert queued["status"] == "queued" and queued["ahead"] == 0

    fake.start_running(pid)
    assert comfy.status(pid)["status"] == "running"

    fake.finish(pid)
    done = comfy.status(pid)
    assert done["status"] == "completed" and done["outputs"]


def test_a_failure_names_the_node_and_the_reason(engine):
    fake, comfy = engine
    pid = comfy.submit(PLAN)["prompt_id"]
    fake.fail(pid, node="KSampler", why="CUDA out of memory")
    state = comfy.status(pid)
    assert state["status"] == "failed"
    assert "KSampler" in state["reason"] and "out of memory" in state["reason"]


def test_a_render_somebody_stopped_is_cancelled_and_not_failed(engine):
    """Found against the real engine. ComfyUI records an interruption in the
    same `completed: false` shape as a crash, so without reading the message
    name every cancel would be reported as "the render failed" — which is a
    small lie that costs somebody a real minute of worry."""
    fake, comfy = engine
    pid = comfy.submit(PLAN)["prompt_id"]
    fake.start_running(pid)
    fake.interrupt(pid)

    state = comfy.status(pid)
    assert state["status"] == "cancelled"
    assert "interrupted" in state["reason"]


def test_a_job_the_engine_has_never_heard_of_is_unknown_not_failed(engine):
    """ComfyUI forgets its history on restart. A job it cannot find is not a
    job that failed, and saying so lets the caller decide what to do."""
    fake, comfy = engine
    state = comfy.status("p-nobody")
    assert state["status"] == "unknown"
    assert "restarted" in state["reason"]


# ── cancelling ────────────────────────────────────────────────────────────

def test_cancelling_a_queued_job_removes_it_from_the_queue(engine):
    fake, comfy = engine
    pid = comfy.submit(PLAN)["prompt_id"]
    out = comfy.cancel(pid)
    assert out["ok"] and out["was"] == "queued" and out["removed_from_queue"]
    assert fake.pending == []
    assert comfy.status(pid)["status"] == "unknown"


def test_cancelling_a_running_job_interrupts_it_and_also_clears_the_queue(engine):
    """Both halves. Interrupting only would leave a job that had not started
    to start a moment later, which reads as "cancel does not work"."""
    fake, comfy = engine
    pid = comfy.submit(PLAN)["prompt_id"]
    fake.start_running(pid)

    out = comfy.cancel(pid)
    assert out["ok"] and out["was"] == "running"
    assert out["interrupted"] and out["removed_from_queue"]
    assert ("POST", "/interrupt", {}) in fake.calls
    assert any(c[0] == "POST" and c[1] == "/queue" and c[2].get("delete") == [pid]
               for c in fake.calls)
    assert fake.running == []


def test_cancelling_something_already_finished_says_so_rather_than_pretending(engine):
    fake, comfy = engine
    pid = comfy.submit(PLAN)["prompt_id"]
    fake.finish(pid)
    out = comfy.cancel(pid)
    assert out["ok"] is False and out["reason"] == "already_completed"


# ── getting the pictures out ──────────────────────────────────────────────

def test_outputs_are_descriptors_until_somebody_wants_the_bytes(engine):
    fake, comfy = engine
    pid = comfy.submit(PLAN)["prompt_id"]
    fake.finish(pid)

    found = comfy.outputs(pid)
    assert len(found) == 1
    assert found[0]["filename"] == "product_00001_.png"
    assert found[0]["node"] == "7"
    # nothing was downloaded just by listing
    assert not any(c[1] == "/view" for c in fake.calls)


def test_a_preview_the_engine_will_delete_itself_is_not_collected(engine):
    fake, comfy = engine
    pid = comfy.submit(PLAN)["prompt_id"]
    fake.history[pid] = {
        "status": {"completed": True, "messages": []},
        "outputs": {"7": {"images": [
            {"filename": "preview.png", "subfolder": "", "type": "temp"},
            {"filename": "real.png", "subfolder": "", "type": "output"}]}}}
    assert [o["filename"] for o in comfy.outputs(pid)] == ["real.png"]


def test_an_unfinished_job_has_no_outputs_rather_than_a_guess(engine):
    fake, comfy = engine
    pid = comfy.submit(PLAN)["prompt_id"]
    assert comfy.outputs(pid) == []


def test_downloading_writes_the_bytes_and_names_the_file_itself(tmp_path, engine):
    """The engine's name is used for its extension and nothing else: a name
    that came from the far end of an HTTP call is not a name to build a path
    out of."""
    fake, comfy = engine
    fake.files["sub/odd name..png"] = b"bytes-here"
    written = comfy.download(
        {"filename": "odd name..png", "subfolder": "sub", "type": "output"},
        into=str(tmp_path))

    assert written.startswith(str(tmp_path))
    assert open(written, "rb").read() == b"bytes-here"
    name = written.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    assert " " not in name and ".." not in name
    # …and it keeps ONE extension. Sanitising the whole name instead of the
    # stem produced `draft_00001_png.png` against the real engine: the dot was
    # stripped and then a new one appended.
    assert name.endswith(".png") and name.count(".") == 1
    assert not name.endswith("png.png")


def test_two_outputs_with_the_same_name_do_not_overwrite_each_other(tmp_path, engine):
    fake, comfy = engine
    fake.files["a.png"] = b"first"
    first = comfy.download({"filename": "a.png", "type": "output"}, into=str(tmp_path))
    fake.files["a.png"] = b"second"
    second = comfy.download({"filename": "a.png", "type": "output"}, into=str(tmp_path))
    assert first != second
    assert open(first, "rb").read() == b"first"
    assert open(second, "rb").read() == b"second"


# ── the registry sees it ──────────────────────────────────────────────────

def test_the_capability_registry_reports_the_engine_it_actually_finds(engine, monkeypatch):
    """The registry's rule from Phase 0: declarations are durable intent,
    observations are disposable facts. `media_worker` is implemented now, so
    the observation has to come from asking a real engine — not from the fact
    that the code exists."""
    from src import capability_registry as registry
    fake, comfy = engine
    monkeypatch.setattr("src.media_backends.ComfyUIBackend",
                        lambda *a, **k: comfy)
    registry._probe_cache.clear()

    seen = registry.observe("media_worker", fresh=True)
    assert seen.state == "available" and "RTX 4070 Ti" in seen.evidence

    fake.checkpoints = []
    comfy._catalogue = None
    registry._probe_cache.clear()
    seen = registry.observe("media_worker", fresh=True)
    assert seen.state == "unavailable" and "no_models" in seen.evidence
