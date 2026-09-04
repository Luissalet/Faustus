"""
media_backends/comfyui.py — ComfyUI as a service, over its own API.

ComfyUI is GPL-3.0. It is reached over HTTP as a separate process and none of
its code is copied here; that is a licence decision that happens to also be
the right architecture, because a render that takes twenty minutes has no
business inside the web process.

Four rules shape this client, and each one is a refusal:

**It never installs anything.** Not a model, not a custom node, not a Python
package — the same rule as the Docker backend, which never pulls an image. A
missing checkpoint comes back as a refusal naming the file, because
downloading six gigabytes because a chat message asked for it is not a
capability anybody agreed to.

**It checks before it queues.** `/object_info` says which nodes exist and
which checkpoints are on disk. Asking costs a moment; a job that fails twenty
minutes in because the checkpoint was spelled differently costs an afternoon,
and the error ComfyUI returns then is about a node's dropdown, not about a
missing file.

**A cancel is two things.** `POST /interrupt` stops what is *running*;
something still in the queue is untouched by it and has to be deleted from
`/queue`. A cancel that only interrupts leaves the job to start ten seconds
later, which reads as "cancel does not work" and is worse than an error.

**A graph never comes from a caller.** It is rendered from an approved
template in `src/media_workflows.py`, and this module will not accept one any
other way. `submit()` takes a graph because the renderer produced it; nothing
here builds one from free text.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: Where ComfyUI listens by default. Loopback: this is a local engine, and a
#: ComfyUI reachable from the network is an unauthenticated file reader and
#: arbitrary-node runner on someone's GPU box.
DEFAULT_BASE_URL = "http://127.0.0.1:8188"

#: How long a single API call may take. Not how long a render may take — a
#: render is polled, never held open on one socket.
CALL_TIMEOUT_S = 15.0

#: Nodes and checkpoints change when someone restarts ComfyUI with new files,
#: not between two calls a second apart.
CATALOGUE_TTL_S = 30.0


class ComfyUIError(RuntimeError):
    """The engine refused, or could not be reached. Carries `reason` so a
    caller can tell "it is not running" from "it does not have that model"
    without reading the sentence."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


class ComfyUIBackend:
    """One ComfyUI service. Sync on purpose — call it from a thread.

    The async version of this would have to own a websocket, a reconnect
    policy and a task per render; polling `/history` from a worker thread is
    duller and survives the engine restarting under it, which the websocket
    does not."""

    id = "media_worker"

    def __init__(self, base_url: str = "", *, timeout: float = CALL_TIMEOUT_S,
                 client_id: str = "faustus") -> None:
        self.base_url = (base_url or os.getenv("COMFYUI_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.client_id = client_id
        self._catalogue: Optional[Tuple[float, Dict[str, Any]]] = None

    # ── talking to it ─────────────────────────────────────────────────────

    def _url(self, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
        return url

    def _call(self, path: str, *, method: str = "GET",
              body: Optional[Mapping[str, Any]] = None,
              params: Optional[Mapping[str, Any]] = None,
              raw: bool = False) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self._url(path, params), data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:2000]
            except Exception:
                pass
            raise ComfyUIError(f"http_{e.code}", detail or str(e)) from e
        except urllib.error.URLError as e:
            # The common one, and worth its own reason: the service simply is
            # not running. Everything above this layer wants to say "start
            # ComfyUI", not "an error occurred".
            raise ComfyUIError("unreachable",
                               f"nothing answered at {self.base_url} ({e.reason})") from e
        except Exception as e:
            raise ComfyUIError("call_failed", f"{type(e).__name__}: {e}") from e

        if raw:
            return payload
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except Exception as e:
            raise ComfyUIError("bad_response",
                               f"the engine answered with something that is not JSON: {e}")

    # ── is it there, and what does it have ────────────────────────────────

    def probe(self) -> Dict[str, Any]:
        """`{ok, reason, detail}` — the same shape the execution backends use.

        Cheapest question first, and the reasons are kept apart because they
        need different answers: not running (start it), running but with no
        checkpoint (put a model in `models/checkpoints`), running and ready."""
        try:
            stats = self._call("/system_stats")
        except ComfyUIError as e:
            if e.reason == "unreachable":
                return {"ok": False, "reason": "backend_unavailable",
                        "detail": f"{e.detail}. Start ComfyUI (on this machine: "
                                  f"D:\\LocalAI\\Start-ComfyUI.ps1) and point Faustus "
                                  f"at it with COMFYUI_URL if it is not on "
                                  f"{DEFAULT_BASE_URL}."}
            return {"ok": False, "reason": "backend_unavailable", "detail": str(e)}

        devices = stats.get("devices") or []
        gpu = devices[0].get("name") if devices else ""
        vram = devices[0].get("vram_total") if devices else None
        detail = f"ComfyUI at {self.base_url}"
        if gpu:
            detail += f" on {gpu}"
        if isinstance(vram, (int, float)) and vram:
            detail += f" ({vram / (1024 ** 3):.1f} GB VRAM)"

        try:
            checkpoints = self.checkpoints()
        except ComfyUIError as e:
            return {"ok": False, "reason": "catalogue_unreadable",
                    "detail": f"{detail}, but /object_info did not answer: {e}"}
        if not checkpoints:
            return {"ok": False, "reason": "no_models",
                    "detail": f"{detail}, but it has no checkpoints. Put a model in "
                              f"ComfyUI's models/checkpoints folder — Faustus does not "
                              f"download models."}
        return {"ok": True, "reason": "ready",
                "detail": f"{detail} · {len(checkpoints)} checkpoint(s)"}

    def object_info(self, *, fresh: bool = False) -> Dict[str, Any]:
        """The node catalogue, briefly cached. This is the biggest response
        the engine gives, and asking for it per submitted job would make the
        check cost more than the thing it protects."""
        now = time.monotonic()
        if not fresh and self._catalogue and now - self._catalogue[0] < CATALOGUE_TTL_S:
            return self._catalogue[1]
        info = self._call("/object_info")
        if not isinstance(info, dict):
            raise ComfyUIError("bad_response", "/object_info was not an object")
        self._catalogue = (now, info)
        return info

    def checkpoints(self, *, fresh: bool = False) -> List[str]:
        """What `CheckpointLoaderSimple` would offer in its dropdown — which
        is exactly the list of checkpoint files the engine can see."""
        info = self.object_info(fresh=fresh)
        node = info.get("CheckpointLoaderSimple") or {}
        options = (((node.get("input") or {}).get("required") or {})
                   .get("ckpt_name") or [])
        return list(options[0]) if options and isinstance(options[0], list) else []

    def missing(self, plan: Mapping[str, Any], *,
                requires_nodes: Optional[List[str]] = None) -> Dict[str, Any]:
        """What this engine would be missing for a rendered plan.

        Answered before anything is queued. Returns `{ok, missing_nodes,
        missing_models, detail}` — and a refusal names the file, because
        "checkpoint not found" and "you have `sd_xl_base_1.0.safetensors` but
        the template asks for `sd_xl_base_1.0.ckpt`" are different afternoons."""
        info = self.object_info()
        wanted_nodes = list(requires_nodes or [])
        for node in (plan.get("graph") or {}).values():
            if isinstance(node, Mapping) and node.get("class_type"):
                wanted_nodes.append(str(node["class_type"]))

        missing_nodes = sorted({n for n in wanted_nodes if n not in info})
        available = set(self.checkpoints())
        missing_models = sorted({
            str(m.get("name")) for m in (plan.get("models") or ())
            if str(m.get("kind") or "checkpoint") == "checkpoint"
            and str(m.get("name")) not in available})

        parts = []
        if missing_nodes:
            parts.append("this ComfyUI has no node called "
                         + ", ".join(repr(n) for n in missing_nodes)
                         + " — install it in ComfyUI yourself; Faustus does not "
                           "install custom nodes")
        if missing_models:
            near = ", ".join(sorted(available)[:6]) or "none at all"
            parts.append("this ComfyUI does not have "
                         + ", ".join(repr(m) for m in missing_models)
                         + f"; what it does have: {near}")
        return {"ok": not (missing_nodes or missing_models),
                "missing_nodes": missing_nodes,
                "missing_models": missing_models,
                "detail": ". ".join(parts)}

    # ── running one ───────────────────────────────────────────────────────

    def submit(self, plan: Mapping[str, Any], *,
               requires_nodes: Optional[List[str]] = None,
               check: bool = True) -> Dict[str, Any]:
        """Queue a rendered plan. Returns `{prompt_id, queued_at, position}`.

        `plan` is what `media_workflows.render()` produced — never a graph
        somebody typed. `check=False` exists for the case where the caller
        already asked `missing()` a moment ago and does not want to ask twice;
        it is not a way to skip the check."""
        graph = plan.get("graph")
        if not isinstance(graph, Mapping) or not graph:
            raise ComfyUIError("empty_graph",
                               "a plan has to carry a rendered graph; this one does not")
        if check:
            gap = self.missing(plan, requires_nodes=requires_nodes)
            if not gap["ok"]:
                raise ComfyUIError("missing_requirements", gap["detail"])

        answer = self._call("/prompt", method="POST",
                            body={"prompt": dict(graph), "client_id": self.client_id})
        errors = answer.get("node_errors") or {}
        if errors:
            # ComfyUI validates the graph and says which node and which field.
            # Passing that through beats "the render failed": the template is
            # ours, so a validation error here is a bug in a file we control.
            raise ComfyUIError("rejected_by_engine", json.dumps(errors)[:2000])
        prompt_id = answer.get("prompt_id")
        if not prompt_id:
            raise ComfyUIError("no_prompt_id",
                               f"the engine accepted the job but named no id: {answer}")
        return {"prompt_id": str(prompt_id),
                "position": answer.get("number"),
                "queued_at": time.time()}

    def status(self, prompt_id: str) -> Dict[str, Any]:
        """Where one job is: `queued`, `running`, `completed`, `failed` or
        `unknown`, with what is known about it.

        `unknown` is a real answer and not folded into the others: ComfyUI
        forgets history on restart, and a job it has never heard of is not the
        same as one that failed. Saying so lets the caller decide."""
        history = self._call(f"/history/{urllib.parse.quote(prompt_id)}")
        entry = (history or {}).get(prompt_id)
        if entry:
            state = (entry.get("status") or {})
            completed = state.get("completed")
            if completed is True:
                return {"status": "completed", "prompt_id": prompt_id,
                        "outputs": entry.get("outputs") or {},
                        "messages": state.get("messages") or []}
            if state.get("status_str") == "error" or completed is False:
                # A job somebody cancelled is not a job that failed. ComfyUI
                # records the interruption as an execution error, so without
                # this every cancel would come back as a failure — and a user
                # who stopped a render on purpose would be told it broke.
                # Seen against the real engine; the imitation server did not
                # reproduce it.
                if _was_interrupted(state):
                    return {"status": "cancelled", "prompt_id": prompt_id,
                            "reason": "the render was interrupted",
                            "messages": state.get("messages") or []}
                return {"status": "failed", "prompt_id": prompt_id,
                        "reason": _first_error(state),
                        "messages": state.get("messages") or []}
            return {"status": "running", "prompt_id": prompt_id,
                    "outputs": entry.get("outputs") or {}}

        queue = self._call("/queue")
        for item in queue.get("queue_running") or []:
            if _queue_id(item) == prompt_id:
                return {"status": "running", "prompt_id": prompt_id}
        for index, item in enumerate(queue.get("queue_pending") or []):
            if _queue_id(item) == prompt_id:
                return {"status": "queued", "prompt_id": prompt_id, "ahead": index}
        return {"status": "unknown", "prompt_id": prompt_id,
                "reason": "the engine has no record of this job; it may have been "
                          "restarted, which clears its history"}

    def cancel(self, prompt_id: str) -> Dict[str, Any]:
        """Stop a job whether it is running or still waiting.

        Both halves, always: `/interrupt` stops the running one, and deleting
        from `/queue` removes it if it had not started. Doing only the first
        is the bug that makes a cancel look ignored — the job simply starts a
        moment later."""
        state = self.status(prompt_id)
        if state["status"] == "completed":
            return {"ok": False, "reason": "already_completed", "prompt_id": prompt_id}

        deleted = False
        try:
            self._call("/queue", method="POST", body={"delete": [prompt_id]})
            deleted = True
        except ComfyUIError as e:
            logger.debug("comfyui queue delete refused: %s", e)

        interrupted = False
        if state["status"] == "running":
            try:
                self._call("/interrupt", method="POST", body={})
                interrupted = True
            except ComfyUIError as e:
                logger.debug("comfyui interrupt refused: %s", e)

        return {"ok": deleted or interrupted, "prompt_id": prompt_id,
                "was": state["status"], "removed_from_queue": deleted,
                "interrupted": interrupted}

    # ── getting the pictures out ──────────────────────────────────────────

    def outputs(self, prompt_id: str) -> List[Dict[str, Any]]:
        """The files a finished job produced, as `{filename, subfolder, type,
        node}` — descriptors, not bytes. Downloading happens once somebody
        decides to keep them."""
        state = self.status(prompt_id)
        if state["status"] != "completed":
            return []
        found: List[Dict[str, Any]] = []
        for node_id, out in (state.get("outputs") or {}).items():
            for key in ("images", "gifs", "videos", "audio"):
                for item in out.get(key) or []:
                    if not isinstance(item, Mapping) or not item.get("filename"):
                        continue
                    if item.get("type") == "temp":
                        continue          # previews the engine will delete itself
                    found.append({"filename": str(item["filename"]),
                                  "subfolder": str(item.get("subfolder") or ""),
                                  "type": str(item.get("type") or "output"),
                                  "node": str(node_id)})
        return found

    def download(self, descriptor: Mapping[str, Any], *, into: str) -> str:
        """Fetch one output into `into`, and return the path written.

        The engine's own filename is used only for its extension. Everything
        else about the name is ours, because a name that came from the far end
        of an HTTP call is not a name to build a path out of."""
        filename = str(descriptor.get("filename") or "")
        if not filename:
            raise ComfyUIError("no_filename", "the descriptor names no file")
        payload = self._call("/view", params={
            "filename": filename,
            "subfolder": descriptor.get("subfolder") or "",
            "type": descriptor.get("type") or "output"}, raw=True)

        stem, ext = os.path.splitext(os.path.basename(filename))
        ext = "".join(c for c in ext.lower() if c.isalnum() or c == ".")[:8] or ".bin"
        # Sanitise the STEM, not the whole name: keeping the extension in it
        # produced `draft_00001_png.png` against the real engine, because the
        # dot was stripped and then a new one appended.
        safe = "".join(c for c in stem if c.isalnum() or c in "-_")[:60] or "output"
        os.makedirs(into, exist_ok=True)
        target = os.path.join(into, f"{safe}{ext}")
        suffix = 1
        while os.path.exists(target):
            target = os.path.join(into, f"{safe}-{suffix}{ext}")
            suffix += 1
        with open(target, "wb") as fh:
            fh.write(payload)
        return target


def _queue_id(item: Any) -> str:
    """ComfyUI's queue entries are positional lists: `[number, prompt_id, ...]`."""
    if isinstance(item, (list, tuple)) and len(item) > 1:
        return str(item[1])
    if isinstance(item, Mapping):
        return str(item.get("prompt_id") or "")
    return ""


def _was_interrupted(state: Mapping[str, Any]) -> bool:
    """Did this job stop because somebody stopped it?

    ComfyUI reports an interruption in the same `completed: false` shape as a
    real failure, and the only thing that tells them apart is the message
    name. Reading it is the difference between "you cancelled that" and "that
    broke"."""
    for message in state.get("messages") or []:
        if isinstance(message, (list, tuple)) and message and \
                str(message[0]) == "execution_interrupted":
            return True
    return False


def _first_error(state: Mapping[str, Any]) -> str:
    """The first thing in ComfyUI's message log that looks like the cause.

    Its messages are `[name, payload]` pairs and the useful one is usually an
    `execution_error` with a node and an exception. Returning that beats
    returning the whole log, which is mostly progress."""
    for message in state.get("messages") or []:
        if not isinstance(message, (list, tuple)) or len(message) < 2:
            continue
        name, payload = str(message[0]), message[1]
        if name in ("execution_error", "execution_interrupted") and isinstance(payload, Mapping):
            node = payload.get("node_type") or payload.get("node_id") or "?"
            why = payload.get("exception_message") or payload.get("exception_type") or name
            return f"{node}: {why}"
    return str(state.get("status_str") or "the engine reported a failure")
