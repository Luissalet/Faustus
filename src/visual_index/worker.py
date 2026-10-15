"""Stand-alone embedding worker for the visual index (``visual_index_python``).

Runs under ANOTHER Python interpreter than Faustus' (one that has ``torch`` with CUDA and ``transformers>=4.57``) and talks to
:class:`src.visual_index.subprocess_embedder.SubprocessEmbedder` over stdin/stdout, one JSON object per line. It imports
nothing from Faustus: only the standard library plus ``numpy``, ``PIL``, ``torch`` and ``transformers`` (the last two are
imported when the model is loaded, never at import time). The model logic is the same as ``Qwen3VLEmbedder``: the chat
template with the document / query instruction, the hidden state of the last non-padding token, L2-normalised.

Pooling and the two instructions follow the open-source PixelRAG embedder (Copyright the PixelRAG authors, Apache License
2.0); reimplemented here, see ``THIRD_PARTY_NOTICES.md``.

Requests (one line each) and replies (one line each, ``{"ok": true, ...}`` or ``{"ok": false, "error": ..., "code": ...}``;
``id`` is echoed when given)::

    {"op": "info", "load"?: false}
    {"op": "load"}
    {"op": "embed_images", "paths": ["a.png", ...], "format"?: "b64" | "list"}
        -> {"ok": true, "dim": 2048, "shape": [n, dim], "dtype": "float32", "vectors_b64": "..."}      (or "vectors": [[...]])
    {"op": "embed_query", "text": "...", "format"?: "b64" | "list"}
        -> {"ok": true, "dim": 2048, "shape": [dim], "dtype": "float32", "vector_b64": "..."}          (or "vector": [...])
    {"op": "close"}

The first line the worker writes is ``{"ok": true, "event": "ready", ...}``. Stdout carries ONLY these JSON lines: at start the
real stdout is duplicated for them and fd 1 is pointed at stderr, so anything a library prints (even from C code) lands in the
log. ``python worker.py --probe`` prints one JSON line describing the interpreter (torch, CUDA, transformers) and exits.

The process is meant to be started with ``CUDA_DEVICE_ORDER=PCI_BUS_ID`` and ``CUDA_VISIBLE_DEVICES=<n>``, so the chosen card
is ``cuda:0`` in here and no other card can be touched. ``VISUAL_WORKER_FAKE=1`` swaps the model for a deterministic fake
(no torch needed) for tests.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

DEFAULT_MODEL = "Qwen/Qwen3-VL-Embedding-2B"
DOCUMENT_INSTRUCTION = "Represent the user's input."
QUERY_INSTRUCTION = "Retrieve images or text relevant to the user's query."
MIN_TRANSFORMERS = (4, 57)
FAKE_DIM = 32


class WorkerError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


def log(message: str) -> None:
    try:
        sys.stderr.write(f"[visual-worker] {message}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def fake_enabled() -> bool:
    return os.environ.get("VISUAL_WORKER_FAKE", "").strip().lower() in ("1", "true", "yes", "on")


def _version_tuple(text: str) -> tuple:
    out = []
    for part in str(text).split(".")[:2]:
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


# --------------------------------------------------------------------------------------------- probe
def probe() -> Dict[str, Any]:
    """What this interpreter offers. Counts the cards without opening any of them."""
    os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    info: Dict[str, Any] = {"ok": True, "python": sys.version.split()[0], "executable": sys.executable, "fake": fake_enabled(),
                            "torch": None, "cuda_available": False, "device_count": 0, "cuda": None, "transformers": None,
                            "numpy": None, "pillow": None, "missing": [], "errors": {}}
    if fake_enabled():
        info.update(torch="fake", transformers="fake", numpy="fake", pillow="fake",
                    cuda_available=int(os.environ.get("VISUAL_WORKER_FAKE_GPUS", "0") or 0) > 0,
                    device_count=int(os.environ.get("VISUAL_WORKER_FAKE_GPUS", "0") or 0), transformers_ok=True)
        return info
    for module, key in (("numpy", "numpy"), ("PIL", "pillow")):
        try:
            mod = __import__(module)
            info[key] = str(getattr(mod, "__version__", "") or getattr(mod, "VERSION", "") or "?")
        except Exception as exc:  # noqa: BLE001
            info["missing"].append("pillow" if module == "PIL" else module)
            info["errors"][module] = f"{type(exc).__name__}: {str(exc)[:200]}"
    try:
        import torch

        info["torch"] = str(torch.__version__)
        info["cuda"] = getattr(torch.version, "cuda", None)
        info["cuda_available"] = bool(torch.cuda.is_available())
        info["device_count"] = int(torch.cuda.device_count()) if info["cuda_available"] else 0
    except Exception as exc:  # noqa: BLE001
        info["missing"].append("torch")
        info["errors"]["torch"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    try:
        import transformers

        info["transformers"] = str(transformers.__version__)
        info["transformers_ok"] = _version_tuple(info["transformers"]) >= MIN_TRANSFORMERS
    except Exception as exc:  # noqa: BLE001
        info["missing"].append("transformers")
        info["transformers_ok"] = False
        info["errors"]["transformers"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return info


# --------------------------------------------------------------------------------------------- models
class FakeModel:
    """Deterministic stand-in (``VISUAL_WORKER_FAKE=1``): the vector of an image is derived from its mean colour, the vector
    of a query from its words, so equal inputs always give equal rows and different ones differ."""

    name = "fake-visual-worker"

    def __init__(self, dim: int = FAKE_DIM):
        self.dim = dim
        self.device = "fake"
        self.dtype = "float32"
        self.loaded = True

    def _vec(self, seed: str):
        import numpy as np

        v = np.zeros(self.dim, dtype=np.float32)
        for word in str(seed).lower().split() or [""]:
            h = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16)
            v[h % self.dim] += 1.0
        return v / max(float(np.linalg.norm(v)), 1e-12)

    def _noise(self) -> None:
        if os.environ.get("VISUAL_WORKER_FAKE_NOISE"):          # tests: libraries that talk on stdout must not break the protocol
            print("noise from print")
            os.write(1, b"noise from a C-level write\n")

    def embed_images(self, images: List[Any]):
        import numpy as np

        self._noise()
        rows = []
        for im in images:
            small = np.asarray(im.convert("RGB").resize((4, 4)), dtype=np.float32).reshape(-1, 3).mean(axis=0)
            rows.append(self._vec("c%d c%d c%d" % tuple(int(x // 16) for x in small)))
        return np.stack(rows).astype(np.float32) if rows else np.zeros((0, self.dim), dtype=np.float32)

    def embed_query(self, text: str):
        return self._vec(text).astype("float32")


class QwenModel:
    """``Qwen/Qwen3-VL-Embedding-2B`` through transformers; same pooling as ``Qwen3VLEmbedder``."""

    def __init__(self, model_id: str, cache_dir: str, device: str, batch: Optional[int]):
        self.name = model_id
        self.cache_dir = cache_dir
        self.device = device
        self.batch = batch
        self.dim = 2048
        self.dtype = ""
        self.loaded = False
        self.torch: Any = None
        self.processor: Any = None
        self.model: Any = None

    def load(self) -> None:
        os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        if os.name == "nt":
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
        try:
            import torch
            import transformers
            from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
        except Exception as exc:  # noqa: BLE001
            raise WorkerError(f"torch/transformers could not be imported for {self.name}: {type(exc).__name__}: {exc}. "
                              f"This interpreter needs torch (CUDA build for a GPU) and transformers>={MIN_TRANSFORMERS[0]}."
                              f"{MIN_TRANSFORMERS[1]}", "dependencies_broken") from exc
        if _version_tuple(transformers.__version__) < MIN_TRANSFORMERS:
            raise WorkerError(f"transformers {transformers.__version__} has no Qwen3-VL; install transformers>="
                              f"{MIN_TRANSFORMERS[0]}.{MIN_TRANSFORMERS[1]}", "dependencies_broken")
        want_cuda = self.device.startswith("cuda")
        if want_cuda and not torch.cuda.is_available():
            raise WorkerError("a GPU was requested but this torch cannot see one (CPU-only build, or the card is not visible); "
                              "use a Python whose torch is a CUDA build", "cuda_unavailable")
        try:
            if not want_cuda:
                dtype = torch.float32
            else:
                major = torch.cuda.get_device_capability(0)[0]            # cuda:0 here is the one card the parent made visible
                dtype = torch.bfloat16 if major >= 8 else torch.float16
            self.dtype = str(dtype).replace("torch.", "")
            os.makedirs(self.cache_dir, exist_ok=True)
            log(f"loading {self.name} on {self.device} ({self.dtype}) from {self.cache_dir}")
            processor = AutoProcessor.from_pretrained(self.name, cache_dir=self.cache_dir)
            try:
                model = Qwen3VLForConditionalGeneration.from_pretrained(
                    self.name, cache_dir=self.cache_dir, dtype=dtype, attn_implementation="sdpa").eval()
            except TypeError:           # transformers < 4.56 only knows the old keyword
                model = Qwen3VLForConditionalGeneration.from_pretrained(
                    self.name, cache_dir=self.cache_dir, torch_dtype=dtype, attn_implementation="sdpa").eval()
            if want_cuda:
                model = model.to(self.device)
            tok = getattr(processor, "tokenizer", None)
            if tok is not None:
                tok.padding_side = "right"          # the pooled token is attention_mask.sum() - 1
        except WorkerError:
            raise
        except Exception as exc:  # noqa: BLE001
            code = "oom" if type(exc).__name__ == "OutOfMemoryError" else "load_failed"
            raise WorkerError(f"could not load {self.name} ({type(exc).__name__}: {str(exc)[:300]}). The weights are fetched from "
                              f"the model hub on first use into {self.cache_dir}.", code) from exc
        self.torch, self.processor, self.model = torch, processor, model
        try:
            self.dim = int(model.config.text_config.hidden_size)
        except Exception:  # noqa: BLE001
            pass
        self.loaded = True

    def _prompt(self, instruction: str, user_content: list) -> str:
        conversation = [{"role": "system", "content": [{"type": "text", "text": instruction}]},
                        {"role": "user", "content": user_content}]
        return self.processor.apply_chat_template(conversation, tokenize=False, add_generation_prompt=True)

    def _pool(self, texts: List[str], images: Optional[List[Any]]):
        torch = self.torch
        kwargs: Dict[str, Any] = {"text": texts, "return_tensors": "pt", "padding": True}
        if images:
            kwargs["images"] = images
        inputs = self.processor(**kwargs)
        inputs = {k: (v.to(self.device) if hasattr(v, "to") else v) for k, v in inputs.items()}
        with torch.no_grad():
            out = self.model.model(**inputs)
            hidden = out.last_hidden_state
            last = inputs["attention_mask"].sum(dim=1) - 1
            pooled = hidden[torch.arange(hidden.size(0), device=hidden.device), last]
            pooled = torch.nn.functional.normalize(pooled.float(), p=2, dim=-1)
        return pooled.cpu().numpy()

    def embed_images(self, images: List[Any]):
        import numpy as np

        if not images:
            return np.zeros((0, self.dim), dtype=np.float32)
        batch = self.batch or (4 if self.device.startswith("cuda") else 1)
        prompt = self._prompt(DOCUMENT_INSTRUCTION, [{"type": "image"}])
        rows = []
        for i in range(0, len(images), batch):
            part = [im.convert("RGB") for im in images[i:i + batch]]
            rows.append(self._pool([prompt] * len(part), part))
        return np.concatenate(rows, axis=0).astype(np.float32)

    def embed_query(self, text: str):
        prompt = self._prompt(QUERY_INSTRUCTION, [{"type": "text", "text": str(text)}])
        return self._pool([prompt], None)[0].astype("float32")


# --------------------------------------------------------------------------------------------- the loop
class Worker:
    def __init__(self, *, model_id: str = DEFAULT_MODEL, cache_dir: str = "", device: str = "cpu", batch: Optional[int] = None):
        self.model_id = model_id
        self.cache_dir = cache_dir or os.path.join(os.getcwd(), "visual-embedding")
        self.device = device
        self.batch = batch
        self.model: Any = None
        self.closing = False

    # ---- model
    def _ensure(self) -> Any:
        if self.model is None:
            if fake_enabled():
                self.model = FakeModel(int(os.environ.get("VISUAL_WORKER_FAKE_DIM", FAKE_DIM) or FAKE_DIM))
            else:
                model = QwenModel(self.model_id, self.cache_dir, self.device, self.batch)
                model.load()
                self.model = model
        return self.model

    def _info(self) -> Dict[str, Any]:
        model = self.model
        info: Dict[str, Any] = {"pid": os.getpid(), "model": self.model_id, "loaded": model is not None,
                                "device": model.device if model is not None else self.device,
                                "dtype": model.dtype if model is not None else "", "dim": model.dim if model is not None else 2048,
                                "fake": fake_enabled(), "cache_dir": self.cache_dir,
                                "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES")}
        if model is not None and not fake_enabled():
            try:
                info["cuda_memory_mb"] = int(model.torch.cuda.memory_allocated(0) // (1024 * 1024)) if self.device.startswith("cuda") else 0
            except Exception:  # noqa: BLE001
                pass
        return info

    # ---- ops
    def handle(self, request: Any) -> Dict[str, Any]:
        if not isinstance(request, dict):
            raise WorkerError("a request must be a JSON object", "invalid_request")
        op = str(request.get("op") or "")
        if op == "ping":
            return {"pong": True}
        if op == "info":
            if request.get("load"):
                self._ensure()
            return self._info()
        if op == "load":
            self._ensure()
            return self._info()
        if op == "embed_images":
            paths = request.get("paths")
            if not isinstance(paths, list):
                raise WorkerError("`paths` must be a list of image files", "invalid_request")
            images = self._open_images([str(p) for p in paths])
            model = self._ensure()
            try:
                vectors = model.embed_images(images)
            finally:
                for im in images:
                    try:
                        im.close()
                    except Exception:  # noqa: BLE001
                        pass
            return self._vectors(vectors, "vectors", request, model)
        if op == "embed_query":
            text = request.get("text")
            if not isinstance(text, str) or not text.strip():
                raise WorkerError("`text` must be a non-empty string", "invalid_request")
            model = self._ensure()
            return self._vectors(model.embed_query(text), "vector", request, model)
        if op == "close":
            self.closing = True
            self.model = None
            return {"closed": True}
        raise WorkerError(f"unknown op {op!r}; expected info, load, embed_images, embed_query or close", "invalid_request")

    @staticmethod
    def _open_images(paths: List[str]) -> List[Any]:
        from PIL import Image

        images = []
        try:
            for path in paths:
                try:
                    with Image.open(path) as im:
                        im.load()
                        images.append(im.convert("RGB"))
                except OSError as exc:
                    raise WorkerError(f"cannot read image {path!r} ({exc})", "unreadable_image") from exc
        except Exception:
            for im in images:
                im.close()
            raise
        return images

    @staticmethod
    def _vectors(array: Any, key: str, request: Dict[str, Any], model: Any) -> Dict[str, Any]:
        import numpy as np

        arr = np.ascontiguousarray(np.asarray(array, dtype="<f4"))
        out: Dict[str, Any] = {"dim": int(model.dim), "shape": list(arr.shape), "dtype": "float32", "device": model.device}
        if str(request.get("format") or "b64") == "list":
            out[key] = arr.tolist()
        else:
            out[key + "_b64"] = base64.b64encode(arr.tobytes()).decode("ascii")
        return out


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, WorkerError):
        return exc.code
    if type(exc).__name__ == "OutOfMemoryError":
        return "oom"
    return "internal"


def serve(worker: Worker, reader: Any, out: Any) -> int:
    """Answer requests from ``reader`` (a binary line iterator) on ``out`` (a text stream) until ``close`` or EOF."""

    def send(payload: Dict[str, Any]) -> None:
        out.write(json.dumps(payload, separators=(",", ":")) + "\n")
        out.flush()

    send({"ok": True, "event": "ready", "pid": os.getpid(), "device": worker.device, "fake": fake_enabled(),
          "python": sys.version.split()[0]})
    for raw in reader:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if not line.strip():
            continue
        req_id = None
        try:
            request = json.loads(line)
            if isinstance(request, dict):
                req_id = request.get("id")
            started = time.time()
            reply = {"ok": True, **worker.handle(request)}
            reply["seconds"] = round(time.time() - started, 3)
        except json.JSONDecodeError as exc:
            reply = {"ok": False, "error": f"the request is not valid JSON ({exc})", "code": "invalid_request"}
        except Exception as exc:  # noqa: BLE001 - never a trace on stdout; the log gets it
            if not isinstance(exc, WorkerError):
                log(traceback.format_exc())
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}" if not isinstance(exc, WorkerError) else str(exc),
                     "code": _error_code(exc)}
        if req_id is not None:
            reply["id"] = req_id
        send(reply)
        if worker.closing:
            break
    return 0


def _isolate_stdout():
    """Keep the real stdout for protocol lines only; point fd 1 (and ``sys.stdout``) at stderr."""
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    real = os.fdopen(os.dup(1), "w", encoding="ascii", errors="backslashreplace", newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return real


def main(argv: Optional[List[str]] = None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != here]       # our siblings must not shadow libraries
    parser = argparse.ArgumentParser(description="Visual index embedding worker (JSON lines on stdin/stdout)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--device", default="cpu", help="'cpu' or 'cuda:0' (the only visible card)")
    parser.add_argument("--batch", type=int, default=0)
    parser.add_argument("--probe", action="store_true", help="print a JSON line about this interpreter and exit")
    args = parser.parse_args(argv)
    out = _isolate_stdout()
    if args.probe:
        try:
            info = probe()
        except Exception as exc:  # noqa: BLE001
            info = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        out.write(json.dumps(info, separators=(",", ":")) + "\n")
        out.flush()
        return 0
    worker = Worker(model_id=args.model, cache_dir=args.cache_dir, device=args.device, batch=args.batch or None)
    return serve(worker, sys.stdin.buffer, out)


if __name__ == "__main__":
    sys.exit(main())
