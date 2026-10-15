"""Embedding strips and text queries into one vector space.

``VisualEmbedder`` is the interface the index talks to; three implementations:

* :class:`Qwen3VLEmbedder` -- ``Qwen/Qwen3-VL-Embedding-2B`` through ``transformers``. Optional: ``torch`` and
  ``transformers`` are NOT in ``requirements.txt``; without them the first use raises
  :class:`EmbedderUnavailable` with the exact install line, and nothing else in Faustus is affected (the module imports
  neither at import time). The weights are downloaded on first use into ``DATA_DIR/models/visual-embedding``.
  Strips and queries go through the model's chat template (document side: system "Represent the user's input.", user =
  the image; query side: system "Retrieve images or text relevant to the user's query.", user = the text), the vector is
  the hidden state of the LAST token after the final norm, L2-normalised (2048 dimensions).
* :class:`subprocess_embedder.SubprocessEmbedder` -- the same model run by ``src/visual_index/worker.py`` under ANOTHER Python
  (setting ``visual_index_python``: the interpreter must have a CUDA ``torch``, ``transformers>=4.57``, Pillow and numpy), for
  when Faustus' own environment must keep a CPU-only ``torch``. :func:`get_embedder` picks it when that setting is set.
* :class:`FakeEmbedder` -- deterministic, no dependencies, for tests.

Where the model runs is decided by :func:`choose_device`: **never GPU 0 or GPU 1** (they are the owner's), GPUs 2 and 3
when one has room (a memory lease from the hub's arbiter, ``hoard_link.lease``, with ``gpus=[2, 3]``), otherwise the
CPU. The model is dropped (and the lease released) after ``visual_index_idle_unload_seconds`` without use.

Design reference for the pooling and the two instructions: the open-source PixelRAG embedder/server
(``embed/src/pixelrag_embed/embed.py``, ``serve/src/pixelrag_serve/api.py``), Copyright the PixelRAG authors, Apache
License 2.0; reimplemented here, see ``THIRD_PARTY_NOTICES.md``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, List, Optional, Protocol, Sequence

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "Qwen/Qwen3-VL-Embedding-2B"
DOCUMENT_INSTRUCTION = "Represent the user's input."
QUERY_INSTRUCTION = "Retrieve images or text relevant to the user's query."
INSTALL_HINT = ('Install the optional packages in the Faustus Python environment: '
                'python -m pip install torch "transformers>=4.57" (the model, about 4.5 GB, downloads on first use)')
#: GPUs that belong to the person who runs Faustus; the visual index never loads anything on them.
RESERVED_GPUS = (0, 1)
DEFAULT_GPUS = (2, 3)
#: VRAM asked of the arbiter for the 2B model in bf16 plus activations for a batch of strips.
DEFAULT_VRAM_MB = 7000


class EmbedderUnavailable(RuntimeError):
    """The embedder cannot run here (packages missing, no model, load failed). ``str()`` says what to do."""

    def __init__(self, message: str, *, code: str = "embedder_unavailable"):
        super().__init__(message)
        self.code = code


class VisualEmbedder(Protocol):
    name: str
    dim: int

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        """``(len(images), dim)`` float32, rows L2-normalised."""

    def embed_query(self, text: str) -> np.ndarray:
        """``(dim,)`` float32, L2-normalised."""

    def close(self) -> None:
        """Free memory (and any GPU lease). Safe to call twice."""


def l2_normalise(vectors: np.ndarray) -> np.ndarray:
    v = np.asarray(vectors, dtype=np.float32)
    norm = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(norm, 1e-12)


# --------------------------------------------------------------------------------------------- settings
def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting

        return get_setting(key, default)
    except Exception:  # noqa: BLE001 - settings unreadable: use the default
        return default


def models_dir() -> str:
    custom = str(_setting("visual_index_models_dir", "") or "").strip()
    if custom:
        return custom
    from src.constants import DATA_DIR

    return os.path.join(DATA_DIR, "models", "visual-embedding")


def allowed_gpus() -> List[int]:
    """The GPU indices the index may use: the configured list minus GPUs 0 and 1, whatever the setting says."""
    raw = _setting("visual_index_gpus", ",".join(str(i) for i in DEFAULT_GPUS))
    if isinstance(raw, str):
        items = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = list(DEFAULT_GPUS)
    out: List[int] = []
    for item in items:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if idx not in RESERVED_GPUS and idx >= 0 and idx not in out:
            out.append(idx)
    return out


# --------------------------------------------------------------------------------------------- device
@dataclass
class DeviceChoice:
    device: str                      # "cpu" or "cuda:N"
    gpu: Optional[int] = None
    lease: Any = None
    reason: str = ""
    via: str = ""                    # "hub" | "local" | ""


def choose_device(*, vram_mb: int = DEFAULT_VRAM_MB, lease_factory: Optional[Callable[..., Any]] = None,
                  gpu_count: Optional[Callable[[], int]] = None) -> DeviceChoice:
    """Pick where the model loads: a leased GPU among :func:`allowed_gpus`, else the CPU. Never raises.

    ``lease_factory(vram_mb, purpose, owner, gpu, timeout_s)`` and ``gpu_count()`` are injectable for tests."""
    mode = str(_setting("visual_index_device", "auto") or "auto").lower()
    if mode == "cpu":
        return DeviceChoice("cpu", reason="visual_index_device is 'cpu'")
    gpus = allowed_gpus()
    if not gpus:
        return DeviceChoice("cpu", reason="no GPU allowed (GPUs 0 and 1 are reserved; visual_index_gpus lists none else)")
    try:
        count = (gpu_count or _torch_gpu_count)()
    except Exception as exc:  # noqa: BLE001
        return DeviceChoice("cpu", reason=f"cannot count GPUs ({exc})")
    usable = [g for g in gpus if g < count]
    if not usable:
        return DeviceChoice("cpu", reason=f"no allowed GPU present (allowed {gpus}, machine has {count})")
    if not bool(_setting("visual_index_gpu_lease", True)):
        return DeviceChoice(f"cuda:{usable[0]}", gpu=usable[0], reason="lease disabled", via="")
    factory = lease_factory or _default_lease
    timeout = float(_setting("visual_index_lease_timeout_seconds", 20) or 20)
    try:
        lease = factory(vram_mb, "visual index embedder", "faustus", usable, timeout)
        entered = lease.__enter__()
    except Exception as exc:  # noqa: BLE001 - LeaseTimeout, hub down, refusal: the CPU still works
        return DeviceChoice("cpu", reason=f"no GPU lease: {exc}")
    gpu = getattr(entered, "gpu", None)
    if gpu is None or gpu in RESERVED_GPUS or gpu not in usable:
        # The arbiter answered with a card we may not use (or none): give it back and go to the CPU.
        try:
            lease.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        return DeviceChoice("cpu", reason=f"the arbiter offered GPU {gpu}, outside the allowed {usable}")
    return DeviceChoice(f"cuda:{gpu}", gpu=int(gpu), lease=lease, via=str(getattr(entered, "via", "") or ""),
                        reason=str(getattr(entered, "warning", "") or ""))


def _torch_gpu_count() -> int:
    os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")      # same numbering as nvidia-smi and the arbiter
    import torch

    return int(torch.cuda.device_count()) if torch.cuda.is_available() else 0


def _default_lease(vram_mb: int, purpose: str, owner: str, gpus: List[int], timeout_s: float) -> Any:
    from src.hoard_link.lease import lease

    return lease(vram_mb=vram_mb, purpose=purpose, owner=owner, gpu=list(gpus), timeout_s=timeout_s)


def release_choice(choice: Optional[DeviceChoice]) -> None:
    if choice is None or choice.lease is None:
        return
    try:
        choice.lease.__exit__(None, None, None)
    except Exception:  # noqa: BLE001
        logger.debug("releasing the GPU lease failed", exc_info=True)
    choice.lease = None


# --------------------------------------------------------------------------------------------- the real model
def dependencies_missing() -> List[str]:
    import importlib.util

    return [m for m in ("torch", "transformers") if importlib.util.find_spec(m) is None]


def model_downloaded(model_id: str = DEFAULT_MODEL, directory: Optional[str] = None) -> bool:
    folder = os.path.join(directory or models_dir(), "models--" + model_id.replace("/", "--"), "snapshots")
    try:
        return any(os.listdir(os.path.join(folder, snap)) for snap in os.listdir(folder))
    except OSError:
        return False


class Qwen3VLEmbedder:
    name = DEFAULT_MODEL
    dim = 2048
    mode = "in_process"

    def __init__(self, model_id: str = DEFAULT_MODEL, *, directory: Optional[str] = None, batch_size: Optional[int] = None,
                 lease_factory: Optional[Callable[..., Any]] = None):
        self.name = model_id
        self.directory = directory or models_dir()
        self._batch = batch_size
        self._lease_factory = lease_factory
        self._lock = threading.RLock()
        self._model: Any = None
        self._processor: Any = None
        self._choice: Optional[DeviceChoice] = None
        self._torch: Any = None

    # ---- loading
    def status(self) -> dict:
        missing = dependencies_missing()
        return {"embedder": self.name, "mode": "in_process", "dim": self.dim, "dependencies_missing": missing,
                "install_hint": INSTALL_HINT if missing else "",
                "model_downloaded": model_downloaded(self.name, self.directory), "models_dir": self.directory,
                "loaded": self._model is not None, "device": self._choice.device if self._choice else "",
                "allowed_gpus": allowed_gpus()}

    def ensure_loaded(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            missing = dependencies_missing()
            if missing:
                raise EmbedderUnavailable(f"The visual index needs {' and '.join(missing)}, which are not installed. {INSTALL_HINT}.",
                                          code="dependencies_missing")
            os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            if os.name == "nt":
                os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")       # same reason as src/embeddings.py
            try:
                import torch
                from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
            except Exception as exc:  # noqa: BLE001 - an old transformers has no Qwen3-VL
                raise EmbedderUnavailable(f"torch/transformers could not be imported for {self.name}: {exc}. {INSTALL_HINT}.",
                                          code="dependencies_broken") from exc
            os.makedirs(self.directory, exist_ok=True)
            choice = choose_device(lease_factory=self._lease_factory)
            try:
                if choice.device == "cpu":
                    dtype = torch.float32
                else:       # asked about the chosen card by index: the default-device helpers would open GPU 0
                    major = torch.cuda.get_device_capability(choice.gpu)[0] if choice.gpu is not None else 0
                    dtype = torch.bfloat16 if major >= 8 else torch.float16
                logger.info("Loading %s on %s (%s)", self.name, choice.device, choice.reason or "ok")
                processor = AutoProcessor.from_pretrained(self.name, cache_dir=self.directory)
                model = Qwen3VLForConditionalGeneration.from_pretrained(
                    self.name, cache_dir=self.directory, torch_dtype=dtype, attn_implementation="sdpa").eval()
                if choice.device != "cpu":
                    model = model.to(choice.device)
                tok = getattr(processor, "tokenizer", None)
                if tok is not None:
                    tok.padding_side = "right"          # the pooled token is attention_mask.sum() - 1
            except Exception as exc:  # noqa: BLE001
                release_choice(choice)
                raise EmbedderUnavailable(f"could not load {self.name} ({type(exc).__name__}: {str(exc)[:300]}). "
                                          f"The weights are fetched from the model hub on first use into {self.directory}.",
                                          code="load_failed") from exc
            self._torch, self._processor, self._model, self._choice = torch, processor, model, choice
            try:
                self.dim = int(model.config.text_config.hidden_size)
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        with self._lock:
            model, torch, choice = self._model, self._torch, self._choice
            self._model = self._processor = self._choice = None
            if model is None:
                return
            device = choice.device if choice else "cpu"
            del model
            try:
                import gc

                gc.collect()
                if torch is not None and device.startswith("cuda"):
                    with torch.cuda.device(device):          # never touch the default device (GPU 0)
                        torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            release_choice(choice)

    # ---- inference
    def _pool(self, texts: List[str], images: Optional[List[Image.Image]]) -> np.ndarray:
        torch = self._torch
        kwargs = {"text": texts, "return_tensors": "pt", "padding": True}
        if images:
            kwargs["images"] = images
        inputs = self._processor(**kwargs)
        device = self._choice.device if self._choice else "cpu"
        inputs = {k: (v.to(device) if hasattr(v, "to") else v) for k, v in inputs.items()}
        with torch.no_grad():
            out = self._model.model(**inputs)
            hidden = out.last_hidden_state
            last = inputs["attention_mask"].sum(dim=1) - 1
            pooled = hidden[torch.arange(hidden.size(0), device=hidden.device), last]
            pooled = torch.nn.functional.normalize(pooled.float(), p=2, dim=-1)
        return pooled.cpu().numpy()

    def _prompt(self, instruction: str, user_content: list) -> str:
        conversation = [{"role": "system", "content": [{"type": "text", "text": instruction}]},
                        {"role": "user", "content": user_content}]
        return self._processor.apply_chat_template(conversation, tokenize=False, add_generation_prompt=True)

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        with self._lock:
            self.ensure_loaded()
            if not images:
                return np.zeros((0, self.dim), dtype=np.float32)
            batch = self._batch or (4 if self._choice and self._choice.device != "cpu" else 1)
            prompt = self._prompt(DOCUMENT_INSTRUCTION, [{"type": "image"}])
            rows = []
            for i in range(0, len(images), batch):
                part = [im.convert("RGB") for im in images[i:i + batch]]
                rows.append(self._pool([prompt] * len(part), part))
            return np.concatenate(rows, axis=0).astype(np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        with self._lock:
            self.ensure_loaded()
            prompt = self._prompt(QUERY_INSTRUCTION, [{"type": "text", "text": str(text)}])
            return self._pool([prompt], None)[0].astype(np.float32)


def get_embedder() -> VisualEmbedder:
    """The real embedder the settings ask for: in a worker process under ``visual_index_python`` when that is set, else the
    model in this process. Cheap: nothing is loaded or started until the first embedding."""
    from .subprocess_embedder import SubprocessEmbedder, configured_python

    python = configured_python()
    if python:
        return SubprocessEmbedder(python)
    return Qwen3VLEmbedder()


def embedder_status(pool: "EmbedderPool", *, probe: bool = False) -> dict:
    """Status of the embedder of ``pool``: the resident one when there is one (so ``device`` and ``loaded`` are real), else
    what a new one would be. ``probe=True`` also runs the interpreter check of ``visual_index_python`` (slow: it imports torch)."""
    current = pool.current()
    if current is None:
        if pool._factory is not None:
            return {"embedder": "custom", "mode": "custom", "loaded": False, "device": ""}
        current = get_embedder()
    status = getattr(current, "status", None)
    if status is None:
        return {"embedder": getattr(current, "name", "custom"), "mode": "custom", "loaded": True, "device": ""}
    out = status(probe=True) if probe and getattr(current, "mode", "") == "subprocess" else status()
    out["resident"] = pool.loaded()
    return out


# --------------------------------------------------------------------------------------------- the fake
_COLOURS = {"red": (220, 40, 40), "green": (40, 190, 60), "blue": (40, 70, 220), "black": (10, 10, 10),
            "white": (245, 245, 245), "yellow": (230, 220, 40)}
_WORD = re.compile(r"[a-z0-9]+")


def colour_name(image: Image.Image) -> str:
    """The nearest of a handful of named colours to the image's mean (what :class:`FakeEmbedder` "sees")."""
    small = image.convert("RGB").resize((8, 8))
    arr = np.asarray(small, dtype=np.float32).reshape(-1, 3).mean(axis=0)
    return min(_COLOURS, key=lambda n: float(np.linalg.norm(arr - np.array(_COLOURS[n], dtype=np.float32))))


class FakeEmbedder:
    """Deterministic bag-of-words embedder: an image is "described" by ``describe(image)`` (default: its nearest named
    colour) and a query by its own words; both land on hashed dimensions, so the query "red" finds red strips."""

    name = "fake-visual-embedder"

    def __init__(self, dim: int = 64, describe: Optional[Callable[[Image.Image], str]] = None):
        self.dim = dim
        self._describe = describe or colour_name
        self.image_calls = 0
        self.query_calls = 0
        self.closed = False

    def _vector(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for word in _WORD.findall(str(text).lower()):
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        if not v.any():
            v[0] = 1.0
        return v

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        self.image_calls += len(images)
        if not images:
            return np.zeros((0, self.dim), dtype=np.float32)
        return l2_normalise(np.stack([self._vector(self._describe(im)) for im in images]))

    def embed_query(self, text: str) -> np.ndarray:
        self.query_calls += 1
        return l2_normalise(self._vector(text))

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------------------------- shared instance + TTL
class EmbedderPool:
    """Holds one embedder, builds it on first use and drops it ``idle_s`` seconds after the last use."""

    def __init__(self, factory: Optional[Callable[[], VisualEmbedder]] = None, *, idle_s: Optional[float] = None):
        self._factory = factory
        self._idle_override = idle_s
        self._lock = threading.RLock()
        self._embedder: Optional[VisualEmbedder] = None
        self._embedder_key = ""
        self._active = 0
        self._timer: Optional[threading.Timer] = None

    def _idle(self) -> float:
        if self._idle_override is not None:
            return float(self._idle_override)
        return float(_setting("visual_index_idle_unload_seconds", 120) or 0)

    def _build(self) -> VisualEmbedder:
        if self._factory is not None:
            return self._factory()
        return get_embedder()

    def _wanted_key(self) -> str:
        """Which kind of embedder the settings ask for now (only meaningful when the pool builds the real one)."""
        if self._factory is not None:
            return ""
        from .subprocess_embedder import configured_python

        return configured_python()

    @contextmanager
    def use(self) -> Iterator[VisualEmbedder]:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._embedder is not None and self._active == 0 and self._embedder_key != self._wanted_key():
                self._unload_now()                      # visual_index_python changed while an embedder was resident
            if self._embedder is None:
                self._embedder_key = self._wanted_key()
                self._embedder = self._build()
            self._active += 1
            embedder = self._embedder
        try:
            yield embedder
        finally:
            with self._lock:
                self._active -= 1
                if self._active == 0:
                    self._schedule_unload()

    def _schedule_unload(self) -> None:
        idle = self._idle()
        if idle <= 0:
            self._unload_now()
            return
        timer = threading.Timer(idle, self._unload_if_idle)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _unload_if_idle(self) -> None:
        with self._lock:
            if self._active == 0:
                self._unload_now()

    def _unload_now(self) -> None:
        embedder, self._embedder, self._timer = self._embedder, None, None
        if embedder is not None:
            try:
                embedder.close()
            except Exception:  # noqa: BLE001
                logger.debug("closing the embedder failed", exc_info=True)

    def unload(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._unload_now()

    def loaded(self) -> bool:
        return self._embedder is not None

    def current(self) -> Optional[VisualEmbedder]:
        return self._embedder


_POOL = EmbedderPool()


def default_pool() -> EmbedderPool:
    return _POOL


def set_pool_for_tests(pool: Optional[EmbedderPool]) -> EmbedderPool:
    """Swap the process-wide pool (tests pass one built on a :class:`FakeEmbedder`); returns the previous one."""
    global _POOL
    previous = _POOL
    _POOL = pool if pool is not None else EmbedderPool()
    return previous
