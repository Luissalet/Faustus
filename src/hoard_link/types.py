"""Small typed value objects returned by :mod:`hoard_link`.

Nothing here does I/O; these are plain dataclasses so apps can log them,
put them in a settings screen, or serialize them straight to JSON.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

Capability = Literal[
    "llm", "vision", "embeddings", "tts", "stt", "image", "video", "music"
]

ApiDialect = Literal["openai", "ollama"]

ResolutionState = Literal["resolved", "unavailable"]

CAPABILITIES: tuple[Capability, ...] = (
    "llm",
    "vision",
    "embeddings",
    "tts",
    "stt",
    "image",
    "video",
    "music",
)


@dataclass(frozen=True)
class Resolution:
    """The answer to "which server and model do I use for capability X".

    ``reason`` is always a short, human-readable sentence suitable for an
    app's Settings screen, e.g.
    ``"llm -> llama.cpp at 127.0.0.1:8081 (qwen3.8-27b-q8-llamacpp), from
    Faustus registry; resident"``.
    """

    capability: str
    provider: Optional[str]
    url: Optional[str]
    model: Optional[str]
    api: Optional[ApiDialect]
    state: ResolutionState
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.state == "resolved"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Usage:
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChatResult:
    text: str
    model: Optional[str]
    provider: Optional[str]
    usage: Usage
    elapsed_ms: float
    reasoning: Optional[str] = None
    effort: Optional[str] = None       # reasoning level the call asked for (None: server default)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass(frozen=True)
class OutputFile:
    """One file referenced by a ComfyUI `/history/{id}` entry."""

    node_id: str
    filename: str
    subfolder: str
    type: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
