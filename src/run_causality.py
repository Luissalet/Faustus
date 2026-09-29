"""Server-owned causal identities; independent of policy and mutable run lookup."""
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class RunOrigin:
    session_id: str
    run_id: str
    _effect_recorder: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class CallOrigin:
    session_id: str
    run_id: Optional[str]
    call_id: Optional[str]


_ORIGIN: ContextVar[Optional[RunOrigin]] = ContextVar("server_run_origin", default=None)


def bind_run(session_id: str, run_id: str, *, _effect_recorder: Any = None) -> Token:
    return _ORIGIN.set(RunOrigin(session_id, run_id, _effect_recorder))


def capture_effect_recorder(session_id: Optional[str]) -> Any:
    """Private execution capability; never part of the exported CallOrigin."""
    origin = _ORIGIN.get()
    return origin._effect_recorder if origin and origin.session_id == str(session_id or "") else None


def reset_run(token: Token) -> None:
    _ORIGIN.reset(token)


def capture_call(session_id: Optional[str], call_id: Optional[str]) -> CallOrigin:
    session = str(session_id or "")
    origin = _ORIGIN.get()
    run_id = origin.run_id if origin and origin.session_id == session else None
    return CallOrigin(session, run_id, str(call_id) if call_id else None)
